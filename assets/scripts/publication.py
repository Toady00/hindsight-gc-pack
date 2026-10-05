"""Publication lifecycle policy: identity, per-document gates, and report sets.

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
import hashlib
import json
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


def check_repository(namespace, documents):
    """Read-only checks shared by `gc hindsight check` and the publisher.

    ``documents`` are dicts with ``relpath`` and the schema ``verdict``. Returns
    ``(errors, warnings)``: lists of ``(relpath, message)``. Retired documents
    still own their IDs, so they take part in the uniqueness check.
    """
    errors, warnings = [], []
    by_id = {}
    fingerprints = {}
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
            if namespace is None or not ref["id"].startswith(namespace + "."):
                errors.append((document["relpath"], f"{verdict['document_id']}: assessed {ref['id']} is outside "
                               "this repository's namespace; reports pin only their own initiative's documents"))
            elif ref["id"] not in fingerprints:
                warnings.append((document["relpath"], f"{verdict['document_id']}: assessed {ref['id']} is not a "
                                 "shippable document here; the report will be held until it is published"))
            elif fingerprints[ref["id"]] != ref["fingerprint"]:
                warnings.append((document["relpath"], f"{verdict['document_id']}: assesses {ref['id']} at "
                                 f"{ref['fingerprint'][:12]}, but the current file is {str(fingerprints[ref['id']])[:12]}; "
                                 "they cannot publish together until the report pins the current revision"))
    return errors, warnings


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Decision:
    def __init__(self, action, reason="", kind=""):
        self.action, self.reason, self.kind = action, reason, kind
        self.set_id = None

    def __repr__(self):
        return f"Decision({self.action!r}, {self.kind!r}, {self.reason!r}, set={self.set_id!r})"


def _refs(entry):
    return {ref["id"]: ref["fingerprint"] for ref in entry.get("assesses") or []}


def _governs(record):
    """Every document a published report has ever pinned. Dropping a pin never releases it."""
    return set(record.get("governs") or []) | set(_refs(record))


def classify(candidate, record, bank_hash, ever_accepted_in_git, staged=False):
    """Per-document gate, before report consistency is considered.

    Returns a Decision: ``unchanged``, ``publish`` (kind ``status`` for a pure
    status/metadata change, ``content`` for a new fingerprint, ``new`` for a
    first publication), ``hold`` (keep the last eligible revision), or
    ``refuse`` (a contract violation needing repair). ``staged`` means this
    pipeline staged a first publication that never completed, so a bank copy
    without a record is its own partial write, not an unknown baseline.
    """
    status = candidate["status"]
    if record is None:
        if bank_hash is not None and not staged:
            return Decision("refuse", "the bank holds this ID but no publication record establishes its "
                            "baseline; nothing can be inferred about its last published revision")
        if status in FROZEN:
            return Decision("refuse", f"a never-published document cannot become {status}: there is no "
                            "published baseline to retire; publish it first or leave it unpublished")
        if status == "draft" and ever_accepted_in_git:
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
    published = f"{record.get('relpath')} at commit {record.get('commit')}"
    if not same and (record.get("status") in FROZEN or status in FROZEN):
        verb = "is frozen" if record.get("status") in FROZEN else f"can become {status} only"
        return Decision("refuse", f"the published revision ({published}) {verb} with unchanged content: "
                        "only status and updated_at may differ from the last published content. Revert the "
                        "file to the published content, restore accepted status if needed, then make "
                        "substantive edits as a draft through reapproval and the build gate")
    if status == "draft" and ever:
        return Decision("hold", "this document has been accepted; draft revisions after acceptance do not "
                        f"publish. The bank keeps {published}")
    return Decision("publish", kind="status" if same else "content")


def plan(candidates, records, bank_hashes, accepted_ids, staged=frozenset()):
    """Decide every candidate and group dependent publications into sets.

    ``staged`` holds IDs whose first publication this pipeline staged but never
    confirmed.

    ``candidates`` maps ID to the scanned candidate (repository, namespace,
    relpath, commit, ref, type, status, fingerprint, source_hash, and for
    reports ``assesses``). ``records`` maps ID to publication records,
    including documents absent from this scan. ``bank_hashes`` maps each ID the
    bank holds to its stored content hash. ``accepted_ids`` holds IDs seen as
    accepted anywhere in published Git history.

    Invariant: after any prefix of the planned writes, every visible report
    only pins fingerprints the bank holds for the documents it assesses. A
    document whose content changes therefore needs every governing report to
    publish an update pinning that exact revision in the same set; acceptance
    alone never unlocks it. Retired and withdrawn reports keep governing.
    """
    decisions = {document_id: classify(candidate, records.get(document_id), bank_hashes.get(document_id),
                                       document_id in accepted_ids, document_id in staged)
                 for document_id, candidate in candidates.items()}
    for document_id, candidate in candidates.items():
        if candidate["type"] != REPORT or decisions[document_id].action not in ("publish", "unchanged"):
            continue
        outside = [ref for ref in _refs(candidate) if not ref.startswith(candidate["namespace"] + ".")]
        if outside:
            decisions[document_id] = Decision("refuse", "a build report pins only its own initiative's "
                                              "documents; outside its namespace: " + ", ".join(sorted(outside)))

    def eligible(document_id):
        return document_id in decisions and decisions[document_id].action == "publish"

    def bank_fingerprint(document_id):
        if eligible(document_id):
            return candidates[document_id]["fingerprint"]
        record = records.get(document_id)
        # A record whose bank copy differs (a failed or unresolved retain)
        # does not describe what readers see.
        if not record or bank_hashes.get(document_id) != record.get("source_hash"):
            return None
        return record.get("fingerprint")

    def report_state(document_id):
        """Post-plan status and pins of a report, if it will exist."""
        if eligible(document_id):
            return candidates[document_id]["status"], _refs(candidates[document_id])
        record = records.get(document_id)
        if record and record.get("type") == REPORT:
            return record.get("status"), _refs(record)
        return None, {}

    reports = {i for i, r in records.items() if r.get("type") == REPORT}
    reports |= {i for i, c in candidates.items() if c["type"] == REPORT}
    changed = True
    while changed:
        changed = False
        for document_id in sorted(reports):
            if not eligible(document_id):
                continue
            stale = [ref for ref, fp in _refs(candidates[document_id]).items() if bank_fingerprint(ref) != fp]
            if stale:
                decisions[document_id] = Decision(
                    "hold", "pins revisions the bank will not hold after this scan: " + ", ".join(sorted(stale))
                    + ". Publish the assessed revisions with it, or pin the revisions already published")
                changed = True
        for document_id in sorted(candidates):
            record = records.get(document_id)
            new = candidates[document_id]["fingerprint"]
            if not eligible(document_id) or not record or new == record.get("fingerprint"):
                continue
            blocking = []
            for report_id in sorted(reports):
                # Every published report that pins this document governs it,
                # retired or currently withdrawn included: retiring a report
                # never releases what it assessed. Only an eligible update that
                # pins this exact revision lets the new content through.
                governing = records.get(report_id)
                if not governing or document_id not in _governs(governing):
                    continue
                status, pins = report_state(report_id)
                if not eligible(report_id) or status in FROZEN or pins.get(document_id) != new:
                    blocking.append(report_id)
            if blocking:
                decisions[document_id] = Decision(
                    "hold", "a published build report assesses this document (" + ", ".join(blocking)
                    + "); its new content publishes only with an updated, evidence-backed report pinning "
                    "this exact revision")
                changed = True

    # Group writes that must become visible as one consistent set.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def content_change(document_id):
        record = records.get(document_id)
        return eligible(document_id) and (not record or record.get("fingerprint") != candidates[document_id]["fingerprint"])

    for report_id in sorted(reports):
        if not eligible(report_id):
            continue
        linked = set(_refs(candidates[report_id]))
        if report_id in records:
            linked |= _governs(records[report_id])
        for ref in linked:
            if content_change(ref) and ref != report_id:
                parent[find(ref)] = find(report_id)
    groups = {}
    for member in list(parent):
        groups.setdefault(find(member), []).append(member)
    sets = []
    for members in groups.values():
        report_ids = sorted(m for m in members if candidates[m]["type"] == REPORT)
        doc_ids = sorted(m for m in members if candidates[m]["type"] != REPORT)
        if not report_ids or not doc_ids:
            continue
        set_id = "set-" + _hash([[m, candidates[m]["fingerprint"], candidates[m]["source_hash"]]
                                 for m in sorted(members)])[:32]
        withdraw = sorted(r for r in report_ids if r in records and records[r].get("visible", True)
                          and _governs(records[r]) & set(doc_ids))
        for member in members:
            decisions[member].set_id = set_id
        sets.append(dict(set_id=set_id, documents=doc_ids, reports=report_ids, withdraw=withdraw))
    return decisions, sorted(sets, key=lambda s: s["set_id"])


def publication_record(candidate, record, set_id, at, ever_accepted_in_git=False):
    """The record that describes a confirmed publication of ``candidate``."""
    previous = record or {}
    result = {key: deepcopy(candidate[key]) for key in (
        "namespace", "repository", "relpath", "commit", "ref", "type", "status", "fingerprint", "source_hash")}
    result.update(
        ever_accepted=bool(previous.get("ever_accepted")) or previous.get("status") == "accepted"
        or candidate["status"] == "accepted" or bool(ever_accepted_in_git),
        visible=True, set=set_id, published_at=at,
        first_published_at=previous.get("first_published_at") or at,
        first_commit=previous.get("first_commit") or candidate["commit"])
    if previous.get("relpath") and previous["relpath"] != candidate["relpath"]:
        result["moved_from"] = previous["relpath"]
    if candidate["type"] == REPORT:
        result["assesses"] = deepcopy(candidate.get("assesses") or [])
        result["governs"] = sorted(_governs(previous) | set(_refs(candidate)))
        result["outcome"] = candidate.get("outcome")
        result["code"] = deepcopy(candidate.get("code") or [])
    return result
