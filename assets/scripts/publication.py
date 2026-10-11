"""Publication lifecycle policy: identity and independent document eligibility.

Pure functions over a scanned manifest and durable publication records. Nothing
here performs I/O; ``ship_docs`` validates the whole plan before any write.

Git holds every revision. The bank holds, per document ID, the last revision
that was eligible to publish. A publication record describes exactly what the
bank was given, and advances only after that retain is confirmed.

Status is per document. The fingerprint covers the whole file except the
values of ``status`` and ``updated_at``, so a pure status change (acceptance
of unchanged content, deprecation, supersession, restoration) keeps it.
"""

from copy import deepcopy
import re

FROZEN = frozenset({"superseded", "deprecated"})
ACTIVE = frozenset({"draft", "accepted"})
RESERVED = frozenset({"rig", "city"})
NAMESPACE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
REPORT = "build-report"
NAMESPACE_FILE = ".hindsight-namespace"


def canonical_namespace(name):
    """One dot-free, case-folded component; None when the name cannot be one."""
    key = name.lower() if isinstance(name, str) else ""
    if not NAMESPACE.fullmatch(key) or key in RESERVED:
        return None
    return key


def namespace_owners(city_name, rig_names):
    """Map each canonical key to its single owner; raise on any collision.

    Gas City rejects duplicate rig names but never compares the city name with
    rig names, and compares case-sensitively. Bank IDs need both checked after
    case folding. Names without a canonical form own no namespace.
    """
    owners, collisions = {}, []
    for name in [city_name, *rig_names]:
        key = canonical_namespace(name)
        if key is None:
            continue
        if key in owners:
            collisions.append(f"{owners[key]!r} and {name!r} both normalize to '{key}'")
            continue
        owners[key] = name
    if collisions:
        raise ValueError("city and rig names collide as document namespaces: " + "; ".join(collisions)
                         + ". Rename one so every city and rig name is unique after lowercasing.")
    return owners


def read_namespace(text):
    """Parse the namespace file: one canonical key and an optional newline."""
    if not isinstance(text, str) or text.count("\n") > 1 or (text.count("\n") == 1 and not text.endswith("\n")):
        raise ValueError(f"{NAMESPACE_FILE} must hold exactly one line")
    key = text.rstrip("\n")
    if canonical_namespace(key) != key:
        raise ValueError(f"{NAMESPACE_FILE} must hold one lowercase letters/digits/_/- key, not 'rig' or 'city'")
    return key


def id_error(document_id, namespace):
    """Return why an ID is outside its repository namespace, or None."""
    parts = document_id.split(".") if isinstance(document_id, str) else []
    if len(parts) < 2 or any(not part for part in parts):
        return "document ID must be <namespace>.<document-id>"
    if namespace is None:
        return f"repository declares no namespace; add {NAMESPACE_FILE} at its root"
    if parts[0] != namespace:
        return f"document ID must start with this repository's namespace '{namespace}.'"
    if parts[1] in RESERVED:
        return "document ID must not insert a literal 'rig' or 'city' component after the namespace"
    return None


def check_repository(namespace, documents, resolve_revision=None):
    """Read-only checks shared by `gc hindsight check` and the publisher.

    ``documents`` are dicts with ``relpath`` and the schema ``verdict``. Returns
    ``(errors, warnings)``: lists of ``(relpath, message)``. Retired documents
    still own their IDs, so they take part in the uniqueness check.
    """
    errors, warnings = [], []
    by_id = {}
    fingerprints = {}
    types = {}
    for document in documents:
        verdict = document["verdict"]
        if verdict.get("verdict") == "skip":
            continue
        document_id = verdict.get("document_id")
        if verdict.get("verdict") == "refuse":
            errors.append((document["relpath"], verdict.get("reason", "schema refused")))
        if not isinstance(document_id, str) or document_id in ("", "?"):
            continue
        by_id.setdefault(document_id, []).append(document["relpath"])
        if verdict.get("verdict") != "ship":
            continue
        problem = id_error(document_id, namespace)
        if problem:
            errors.append((document["relpath"], f"{document_id}: {problem}"))
        lifecycle = verdict.get("lifecycle") or {}
        fingerprints[document_id] = lifecycle.get("fingerprint")
        types[document_id] = lifecycle.get("type")
    for document_id, paths in by_id.items():
        if len(paths) > 1:
            for path in paths:
                errors.append((path, f"{document_id}: ID is claimed by {len(paths)} files: {', '.join(sorted(paths))}"))
    for document in documents:
        verdict = document["verdict"]
        lifecycle = verdict.get("lifecycle") or {}
        if verdict.get("verdict") != "ship" or lifecycle.get("type") != REPORT:
            continue
        for ref in lifecycle.get("assesses", []):
            kind = types.get(ref["id"]) if fingerprints.get(ref["id"]) == ref["fingerprint"] else None
            if kind is None and resolve_revision is not None:
                try:
                    kind = resolve_revision(ref["id"], ref["fingerprint"])
                except (ValueError, OSError) as error:
                    errors.append((document["relpath"], f"cannot verify assessed revision {ref['id']}: {error}"))
                    continue
            if kind == "discussion":
                errors.append((document["relpath"], f"build report assesses discussion {ref['id']}; remove that "
                               "assesses entry and republish the report, because discussions add no requirements"))
            elif namespace is None or not ref["id"].startswith(namespace + "."):
                errors.append((document["relpath"], f"{verdict['document_id']}: assessed {ref['id']} is outside "
                               "this repository's namespace; reports pin only their own initiative's documents"))
            elif kind is None:
                errors.append((document["relpath"], f"{verdict['document_id']}: assessed revision {ref['id']} "
                               f"at {ref['fingerprint']} is not a valid current or historical document in this "
                               "repository; verify the ID and fingerprint against the assessed Git revision"))
    return errors, warnings


class Decision:
    def __init__(self, action, reason="", kind=""):
        self.action, self.reason, self.kind = action, reason, kind
        self.set_id = None

    def __repr__(self):
        return f"Decision({self.action!r}, {self.kind!r}, {self.reason!r}, set={self.set_id!r})"


def _refs(entry):
    return {ref["id"]: ref["fingerprint"] for ref in entry.get("assesses") or []}


def classify(candidate, record, bank_hash, ever_accepted_in_git, staged=False):
    """Per-document gate, before report consistency is considered.

    Returns a Decision: ``unchanged``, ``publish`` (kind ``status`` for a pure
    status/metadata change, ``content`` for a new fingerprint, ``new`` for a
    first publication), ``hold`` (keep the last eligible revision), or
    ``refuse`` (a contract violation needing repair). ``staged`` means this
    pipeline staged a first publication that never completed, so a bank copy
    without a record is its own partial write, not an unknown baseline.
    """
    status = candidate.get("status")
    discussion = candidate["type"] == "discussion"
    if discussion and "status" in candidate:
        return Decision("refuse", "discussion records must omit status")
    if record is None:
        if bank_hash is not None and not staged:
            return Decision("refuse", "the bank holds this ID but no publication record establishes its "
                            "baseline; nothing can be inferred about its last published revision")
        if status in FROZEN:
            return Decision("refuse", f"a never-published document cannot become {status}: there is no "
                            "published baseline to retire; publish it first or leave it unpublished")
        if not discussion and status == "draft" and ever_accepted_in_git:
            return Decision("hold", "this document was accepted in published Git history, and later "
                            "drafts do not publish; accept a revision to publish it")
        return Decision("publish", kind="new")
    # The namespace claim binds a namespace to one repository, so a matching
    # namespace is the identity check; the recorded repository is lineage.
    if record.get("namespace") != candidate["namespace"]:
        return Decision("refuse", f"ID was first published in namespace {record.get('namespace')}; IDs are "
                        "immutable and never reassigned")
    if record.get("type") != candidate["type"]:
        return Decision("refuse", f"ID was published as type {record.get('type')}; an ID is never "
                        "reassigned to a different document")
    ever = bool(record.get("ever_accepted")) or record.get("status") == "accepted" or ever_accepted_in_git
    same = candidate["fingerprint"] == record.get("fingerprint")
    if (record.get("visible", True) and same and candidate["source_hash"] == record.get("source_hash")
            and bank_hash == record.get("source_hash") and candidate["relpath"] == record.get("relpath")):
        return Decision("unchanged")
    if discussion:
        # Legacy status/ever_accepted never invalidates a conversation that happened.
        return Decision("publish", kind="status" if same else "content")
    published = f"{record.get('relpath')} at commit {record.get('commit')}"
    if not same and (record.get("status") in FROZEN or status in FROZEN):
        verb = "is frozen" if record.get("status") in FROZEN else f"can become {status} only"
        return Decision("refuse", f"the published revision ({published}) {verb} with unchanged content: "
                        "only status and updated_at may differ from the last published content. Revert the "
                        "file to the published content, restore accepted status if needed, then make "
                         "substantive edits as a draft through reapproval")
    if status == "draft" and ever:
        return Decision("hold", "this document has been accepted; draft revisions after acceptance do not "
                        f"publish. The bank keeps {published}")
    return Decision("publish", kind="status" if same else "content")


def plan(candidates, records, bank_hashes, accepted_ids, staged=frozenset()):
    """Decide each candidate independently after repository reference validation.

    ``staged`` holds IDs whose first publication this pipeline staged but never
    confirmed.

    ``candidates`` maps ID to the scanned candidate (repository, namespace,
    relpath, commit, ref, type, status, fingerprint, source_hash, and for
    reports ``assesses``). ``records`` maps ID to publication records,
    including documents absent from this scan. ``bank_hashes`` maps each ID the
    bank holds to its stored content hash. ``accepted_ids`` holds IDs seen as
    accepted anywhere in published Git history.

    Reports describe assessed Git revisions, which may be historical and need
    not be the revisions currently in the bank. They never govern publication
    of intent. The empty set list keeps legacy callers compatible.
    """
    decisions = {document_id: classify(candidate, records.get(document_id), bank_hashes.get(document_id),
                                       document_id in accepted_ids, document_id in staged)
                 for document_id, candidate in candidates.items()}
    for document_id, candidate in candidates.items():
        if candidate["type"] != REPORT or decisions[document_id].action not in ("publish", "unchanged"):
            continue
        outside = [ref for ref in _refs(candidate) if not ref.startswith(candidate["namespace"] + ".")]
        discussions = [ref for ref, fp in _refs(candidate).items()
                       if any(entry.get("type") == "discussion" and entry.get("fingerprint") == fp
                              for entry in (candidates.get(ref) or {}, records.get(ref) or {}))]
        if discussions:
            decisions[document_id] = Decision("refuse", "build reports cannot assess discussion records: "
                                              + ", ".join(sorted(discussions))
                                              + "; remove those assesses entries and republish the report")
            continue
        if outside:
            decisions[document_id] = Decision("refuse", "a build report pins only its own initiative's "
                                              "documents; outside its namespace: " + ", ".join(sorted(outside)))

    return decisions, []


def publication_record(candidate, record, set_id, at, ever_accepted_in_git=False):
    """The record that describes a confirmed publication of ``candidate``."""
    previous = record or {}
    result = {key: deepcopy(candidate[key]) for key in (
        "namespace", "repository", "relpath", "commit", "ref", "type", "fingerprint", "source_hash")}
    if candidate["type"] != "discussion":
        result["status"] = candidate["status"]
    result.update(
        visible=True, set=set_id, published_at=at,
        first_published_at=previous.get("first_published_at") or at,
        first_commit=previous.get("first_commit") or candidate["commit"])
    if candidate["type"] != "discussion":
        result["ever_accepted"] = (bool(previous.get("ever_accepted")) or previous.get("status") == "accepted"
                                   or candidate["status"] == "accepted" or bool(ever_accepted_in_git))
    if previous.get("relpath") and previous["relpath"] != candidate["relpath"]:
        result["moved_from"] = previous["relpath"]
    if candidate["type"] == REPORT:
        result["assesses"] = deepcopy(candidate.get("assesses") or [])
        result["outcome"] = candidate.get("outcome")
        result["code"] = deepcopy(candidate.get("code") or [])
    return result
