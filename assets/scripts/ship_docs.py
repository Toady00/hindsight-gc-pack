"""Git-only document shipping with durable, completion-aware Beads receipts."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
from connection import resolve
from git_snapshot import _git, snapshot
from ingestion import API, BeadsStore, Error, Ingestor, _payload_hash, now, require_writer, ship_lock
import publication
from ship_report import ScanReport
from revision_history import RevisionHistory

PACK = Path(__file__).resolve().parents[2]


def load_registry(city):
    """Read the city's rig registry: city name and every rig's name and path."""
    try:
        result = subprocess.run([os.environ.get("GC_BIN") or "gc", "rig", "list", "--json", "--city", city],
                                capture_output=True, text=True, timeout=120, check=True)
        registry = json.loads(result.stdout)
        rigs = registry["rigs"]
        if not isinstance(rigs, list) or any(not isinstance(r, dict) or not isinstance(r.get("name"), str)
                                           or not isinstance(r.get("path"), str) or not r["path"] for r in rigs):
            raise ValueError("invalid rigs")
        city_name = registry.get("city_name") or next((r["name"] for r in rigs if r.get("hq")), "")
        if not isinstance(city_name, str):
            raise ValueError("invalid city name")
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise Error("cannot discover rig docs roots from the city registry") from None
    return dict(city_name=city_name, rigs=rigs)


def discover_roots(city, registry=None):
    rigs = (registry or load_registry(city))["rigs"]
    # Include rig roots even when docs/ is absent in this checkout. The fetched
    # tree determines what exists; a private checkout cannot hide published docs.
    roots = []
    for rig in rigs:
        path = Path(rig["path"]).resolve()
        try:
            if not path.is_dir():
                raise ValueError("missing rig checkout")
            top = Path(_git(path, "rev-parse", "--show-toplevel").decode().rstrip("\n")).resolve()
            if top != path:
                raise ValueError("rig belongs to a parent repository")
        except ValueError:
            raise Error(f"registered rig {rig['name']} must have its own Git checkout at {path}") from None
        roots.append(str(path / "docs"))
    try:
        city_git = _git(city, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except ValueError:
        city_git = False
    if (Path(city) / "docs").exists() or city_git:
        roots.append(str(Path(city) / "docs"))
    for root in shlex.split(os.environ.get("HINDSIGHT_DOCS_ROOTS", "")):
        roots.append(str(Path(city) / root))
    os.environ["HINDSIGHT_KNOWN_REPOS"] = "\n".join(r["name"] for r in rigs)
    return list(dict.fromkeys(roots))


def owners(city, registry):
    """Map each registered checkout to the city or rig name that owns it."""
    result = {}
    for rig in registry["rigs"]:
        result[str(Path(rig["path"]).resolve())] = rig["name"]
    if registry["city_name"]:
        result[str(Path(city).resolve())] = registry["city_name"]
    return result


def derive(document, executable):
    try:
        result = subprocess.run([str(executable)], input=document["content"], text=True,
                                capture_output=True, timeout=120, check=True)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        verdict = json.loads(result.stdout)
        if not isinstance(verdict, dict) or verdict.get("verdict") not in ("skip", "refuse", "ship"):
            raise ValueError("invalid schema verdict")
        if verdict.get("document_id") is not None and not isinstance(verdict["document_id"], str):
            raise ValueError("schema document_id must be a string")
        if verdict["verdict"] != "ship":
            return verdict
        if not isinstance(verdict.get("document_id"), str) or not verdict["document_id"]:
            raise ValueError("schema must emit a nonempty document_id")
        tags = verdict.get("tags")
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ValueError("schema must emit string tags")
        statuses = [tag for tag in tags if tag.startswith("status:")]
        lifecycle = verdict.get("lifecycle")
        discussion = isinstance(lifecycle, dict) and lifecycle.get("type") == "discussion"
        if discussion and (statuses or "status" in verdict["lifecycle"]):
            return dict(verdict="refuse", document_id=verdict["document_id"],
                        reason="discussion records must omit status tags and lifecycle status")
        if not discussion and (len(statuses) != 1 or statuses[0] not in {"status:" + s for s in ("draft", "accepted", "superseded", "deprecated")}):
            return dict(verdict="refuse", document_id=verdict["document_id"],
                        reason="schema must emit exactly one valid status tag")
        problem = lifecycle_error(verdict.get("lifecycle"), None if discussion else statuses[0][len("status:"):])
        if problem:
            return dict(verdict="refuse", document_id=verdict["document_id"], reason=problem)
        return verdict
    except subprocess.CalledProcessError:
        raise Error("schema derive failed", 1) from None
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise Error(f"schema derive failed: {error}", 1) from None


def lifecycle_error(lifecycle, status):
    """Why a schema's optional lifecycle output is unusable, or None."""
    if lifecycle is None:
        return None
    if not isinstance(lifecycle, dict) or lifecycle.get("status", status) != status:
        return "schema lifecycle must be an object agreeing with the status tag"
    if "fingerprint" in lifecycle and not (isinstance(lifecycle["fingerprint"], str)
                                           and re.fullmatch(r"[0-9a-f]{64}", lifecycle["fingerprint"])):
        return "schema lifecycle fingerprint must be 64 lowercase hex characters"
    if lifecycle.get("type") == publication.REPORT:
        assesses = lifecycle.get("assesses")
        if (not isinstance(assesses, list) or any(
                not isinstance(ref, dict) or not isinstance(ref.get("id"), str)
                or not isinstance(ref.get("fingerprint"), str) for ref in assesses)):
            return "schema lifecycle assesses must list {id, fingerprint} objects"
        if len({ref["id"] for ref in assesses}) != len(assesses):
            return "schema lifecycle assesses must not repeat an ID"
    return None


def item_from(document, verdict):
    lines = document["content"].splitlines(keepends=True)
    if "content" in verdict:
        body = verdict["content"]
    else:
        end = next((i for i in range(1, len(lines)) if re.fullmatch(r"---\s*", lines[i])), None)
        if not lines or lines[0].strip() != "---" or end is None:
            raise Error("schema must supply content for a document without frontmatter", 1)
        body = "".join(lines[end + 1:])
    if not isinstance(body, str) or not body.strip():
        raise Error("document has no retainable content", 1)
    source_hash = hashlib.sha256(document["content"].encode("utf-8")).hexdigest()
    source = {key: document[key] for key in ("repository", "relpath", "ref", "commit")}
    source["kind"] = "git"
    item = {key: verdict.get(key) for key in ("document_id", "context", "timestamp", "strategy", "tags", "observation_scopes")}
    item.update(content=body, metadata=dict(content_hash=source_hash, repo=document["repo"],
                                          repository=document["repository"], relpath=document["relpath"],
                                          source_commit=document["commit"], source_ref=document["ref"]))
    fingerprint = (verdict.get("lifecycle") or {}).get("fingerprint") or source_hash
    item["metadata"]["fingerprint"] = fingerprint
    item["context"] = ((item.get("context") or "") + f"; document {verdict['document_id']} at fingerprint "
                       f"{fingerprint}")
    return item, source_hash, source


def candidate_from(document, verdict, source_hash, namespace):
    """The lifecycle view of one scanned document."""
    statuses = [tag[len("status:"):] for tag in verdict["tags"] if tag.startswith("status:")]
    lifecycle = verdict.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        raise Error("schema lifecycle must be an object", 1)
    discussion = lifecycle.get("type") == "discussion"
    status = None if discussion else statuses[0]
    if (discussion and (statuses or "status" in lifecycle)
            or not isinstance(lifecycle, dict) or lifecycle.get("status", status) != status):
        raise Error("schema lifecycle status disagrees with its status tag", 1)
    fingerprint = lifecycle.get("fingerprint") or source_hash
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise Error("schema emitted an invalid lifecycle fingerprint", 1)
    candidate = dict(namespace=namespace, repository=document["repository"], relpath=document["relpath"],
                     commit=document["commit"], ref=document["ref"], type=lifecycle.get("type") or "",
                      fingerprint=fingerprint, source_hash=source_hash)
    if not discussion:
        candidate["status"] = status
    if candidate["type"] == publication.REPORT:
        if lifecycle_error(lifecycle, statuses[0]):
            raise Error("schema emitted an invalid build-report lifecycle", 1)
        candidate.update({key: lifecycle.get(key) for key in ("assesses", "outcome", "code")})
    return candidate


def resolve_namespaces(roots, owner_of, claims, published=None):
    """Validate each scanned repository's namespace against durable claims.

    The first publication must use the canonical city or rig name of the
    checkout's owner. After that the claim binds the key to the repository, so
    renaming the rig changes nothing and no other repository can take the key.
    """
    claimed_by_repo = {}
    for key, claim in claims.items():
        claimed_by_repo.setdefault(claim.get("repository"), set()).add(key)
    result = {}
    for root in roots:
        repository = root.get("repository")
        if root.get("status") != "ok" or repository in result:
            continue
        try:
            if root.get("namespace") is None:
                raise ValueError(f"repository has no {publication.NAMESPACE_FILE}; add one line naming its "
                                 "city or rig namespace before publishing")
            key = publication.read_namespace(root["namespace"])
            claim = claims.get(key)
            if claim and claim.get("repository") != repository:
                raise ValueError(f"namespace '{key}' was first published by {claim.get('repository')}; if that "
                                 "repository moved, rebind the hindsight-namespace record explicitly")
            other = sorted(claimed_by_repo.get(repository, set()) - {key})
            if other:
                raise ValueError(f"this repository already publishes as namespace '{other[0]}'; a namespace "
                                 "and its document IDs never change after first publication")
            owner = owner_of.get(root.get("toplevel", ""))
            others = sorted((published or {}).get(key, set()) - {repository})
            if not claim and others:
                raise ValueError(f"namespace '{key}' has documents published from {others[0]} but no claim; "
                                 "restore its hindsight-namespace claim before publishing from another origin")
            if not claim:
                if owner is None:
                    raise ValueError("repository is neither the city nor a registered rig, so it owns no namespace")
                expected = publication.canonical_namespace(owner)
                if expected is None:
                    raise ValueError(f"{owner!r} has no canonical namespace form; use lowercase letters, "
                                     "digits, '_' or '-', and not 'rig' or 'city'")
                if key != expected:
                    raise ValueError(f"first publication must use the owner's canonical name '{expected}', not '{key}'")
            result[repository] = dict(key=key, error=None, claim=None if claim else dict(
                repository=repository, owner=owner, toplevel=root.get("toplevel")))
        except ValueError as error:
            result[repository] = dict(key=None, error=str(error), claim=None)
    return result


def _publication(data):
    record = data.get("publication")
    if record is None:
        return None
    if (not isinstance(record, dict) or not isinstance(record.get("fingerprint"), str)
            or (not (record.get("type") == "discussion" and "status" not in record)
                and record.get("status") not in ("draft", "accepted", "superseded", "deprecated"))
            or not isinstance(record.get("repository"), str)):
        raise Error(f"invalid publication record for {data.get('document_id')}; manual inspection required")
    return record


def promote(store, document_id):
    """Advance the publication record once the staged retain is confirmed.

    The intended record is staged with the attempt, so a retain completed by a
    later scan's recovery still advances exactly the record it was planned
    with. Nothing advances on an unconfirmed or failed attempt.
    """
    state = store.get("document", document_id) or {}
    pending, receipt = state.get("publication_pending"), state.get("last_success") or {}
    attempt = state.get("attempt") or {}
    if (not isinstance(pending, dict) or attempt.get("state") != "succeeded"
            or receipt.get("source_hash") != pending.get("source_hash")):
        return False
    state["publication"] = pending
    state.pop("publication_pending", None)
    state.pop("withdrawal", None)
    store.put("document", document_id, state)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", default=os.environ.get("HINDSIGHT_BANK", ""))
    parser.add_argument("--api", default="")
    parser.add_argument("--ref", default="", help="published branch on origin; defaults to origin's current default branch")
    parser.add_argument("--fetch", action="store_true", help="accepted for existing callers; every scan now fetches")
    parser.add_argument("--schema", default=os.environ.get("HINDSIGHT_SCHEMA") or str(PACK / "schemas/docs"))
    parser.add_argument("--domains", default="")
    parser.add_argument("--drain-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reprocess", action="store_true")
    parser.add_argument("--full-scan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--discovery-error", default="", help=argparse.SUPPRESS)
    parser.add_argument("roots", nargs="*")
    args = parser.parse_args()
    # Unexpected exceptions must not let the report finalizer manufacture success.
    code, report, started = 5, None, False
    resources = ExitStack()
    try:
        if not args.bank or args.drain_timeout < 0:
            raise Error("set HINDSIGHT_BANK or --bank; drain timeout must be nonnegative", 2)
        if not args.dry_run:
            require_writer()
        try:
            connection = resolve(args.api)
        except (ValueError, OSError):
            raise Error("invalid or missing Hindsight connection configuration", 2) from None
        os.environ.update(HINDSIGHT_API=connection["api"], HINDSIGHT_API_URL=connection["api"],
                          HINDSIGHT_API_KEY=connection["key"], HINDSIGHT_KNOWN_DOMAINS_FILE=args.domains)
        store = BeadsStore(connection["api"], args.bank)
        api = API(connection["api"], args.bank)
        work_id = ""
        if not args.dry_run:
            resources.enter_context(ship_lock(store))
            work_id = store.current_work()
        ingestor = Ingestor(store, api, work_id=work_id)
        full = args.full_scan or not args.roots
        report = ScanReport(store, full, work_id=work_id)
        counts = dict(shipped=0, skipped=0, held=0, withdrawn=0, refused=0, failed=0, gone=0,
                      incomplete=0, recovered=0)
        report.run["counts"] = counts

        def finding(document_id, status, detail, path=""):
            report.run["documents"].append(dict(id=document_id, status=status, detail=detail, path=path))
            print(f"{status.upper():9} {document_id}: {detail}")

        if not args.dry_run:
            bead_id = report.start()
            started = True
            print(f"scan record: {bead_id}")
        if args.discovery_error:
            raise Error(args.discovery_error)
        registry = load_registry(store.city)
        try:
            publication.namespace_owners(registry["city_name"],
                                         [r["name"] for r in registry["rigs"] if not r.get("hq")])
        except ValueError as error:
            raise Error(str(error), 2) from None
        roots = args.roots or discover_roots(store.city, registry)
        if not roots:
            raise Error("no docs roots configured; add a Git rig or pass a Git docs root")
        result = snapshot(roots, args.ref)
        report.run["roots"] = [{k: v for k, v in root.items() if k != "accepted_ids"} for root in result["roots"]]
        counts["incomplete"] = result["errors"]
        for root in result["roots"]:
            print(f"root {root['status']}: {root['root']} {root['ref']} {root['commit']} {root['detail']}")
        if result["errors"]:
            raise Error("incomplete Git snapshot; fix repository, origin, branch, or traversal errors; nothing retained")
        executable = Path(args.schema).resolve() / "derive"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise Error("schema has no executable derive", 2)

        claims = {}
        for claim in store.list_records("namespace"):
            claims[claim["document_id"]] = claim
        published = {}
        for state in store.list_documents():
            record = state.get("publication") or {}
            if record.get("namespace"):
                published.setdefault(record["namespace"], set()).add(record.get("repository"))
        namespaces = resolve_namespaces(result["roots"], owners(store.city, registry), claims, published)
        accepted_ids = {i for root in result["roots"] for i in root.get("accepted_ids", [])}

        # Validate the entire manifest before submitting anything. A duplicate ID
        # refuses both files, rather than retaining whichever happens to come first.
        candidates, items, seen, duplicates = {}, {}, set(), set()
        scanned = {}
        for document in result["documents"]:
            display = f"{document['repo']}@{document['commit']}:{document['relpath']}"
            try:
                verdict = derive(document, executable)
            except Error as error:
                counts["failed"] += 1
                finding("?", "failed", str(error), display)
                continue
            scanned.setdefault(document["repository"], []).append(
                dict(relpath=document["relpath"], verdict=verdict, document=document, display=display))
            document_id = verdict.get("document_id")
            if document_id and document_id != "?":
                if document_id in seen:
                    duplicates.add(document_id)
                seen.add(document_id)
        for repository, documents in scanned.items():
            namespace = namespaces.get(repository, dict(key=None, error="repository namespace unresolved"))
            root = next(r for r in result["roots"] if r["repository"] == repository and r["status"] == "ok")
            history = RevisionHistory(root["toplevel"], root["commit"], derive, executable)
            errors, warnings = publication.check_repository(namespace["key"], documents, history.lookup)
            problems = {}
            for relpath, message in errors:
                problems.setdefault(relpath, []).append(message)
            for relpath, message in warnings:
                print(f"WARN      {relpath}: {message}", file=sys.stderr)
            for entry in documents:
                verdict, document, display = entry["verdict"], entry["document"], entry["display"]
                document_id = verdict.get("document_id") or "?"
                if verdict["verdict"] == "skip" or document_id in duplicates:
                    continue
                reasons = problems.get(entry["relpath"], [])
                if verdict["verdict"] == "ship" and namespace["error"]:
                    reasons = [namespace["error"]] + reasons
                if verdict["verdict"] == "refuse" or reasons:
                    counts["refused"] += 1
                    finding(document_id, "refused", "; ".join(dict.fromkeys(reasons))
                            or verdict.get("reason", "schema refused"), display)
                    continue
                try:
                    item, source_hash, source = item_from(document, verdict)
                    candidates[document_id] = candidate_from(document, verdict, source_hash, namespace["key"])
                    items[document_id] = (item, source_hash, source, display)
                except Error as error:
                    counts["failed"] += 1
                    finding(document_id, "failed", str(error), display)
        for document_id in sorted(duplicates):
            candidates.pop(document_id, None)
            items.pop(document_id, None)
            counts["refused"] += 1
            finding(document_id, "refused", "multiple published files claim this document ID")

        if not args.dry_run:
            api.drain(args.drain_timeout)
        # Recover even records whose source moved or disappeared. Pending payloads
        # live in Beads, so a handoff does not need the previous machine's checkout.
        recovered = {}
        if not args.dry_run:
            for record in store.list_documents():
                if record.get("publication_pending"):
                    # A planned publication is resolved, never retried, before
                    # planning: a failed one is retried only if this scan's plan
                    # selects the same revision again.
                    try:
                        if ingestor.recover(record["document_id"], retry=False, replay=False):
                            recovered[record["document_id"]] = record.get("attempt") or {}
                            counts["recovered"] += 1
                            finding(record["document_id"], "recovered", "prior operation completed and receipt confirmed")
                    except Error as error:
                        if error.code != 1:
                            raise
                        if record["document_id"] not in candidates:
                            counts["failed"] += 1  # nothing in this scan can settle it
                        finding(record["document_id"], "unresolved", f"planned publication failed: {error}")
                elif ingestor.recover(record["document_id"]):
                    recovered[record["document_id"]] = record.get("attempt") or {}
                    counts["recovered"] += 1
                    finding(record["document_id"], "recovered", "prior operation completed and receipt confirmed")
                if promote(store, record["document_id"]):
                    finding(record["document_id"], "recovered", "staged publication record confirmed")
                # Legacy prepared withdrawals are not resumed: historical reports
                # remain valid. Missing copies are restored by ordinary publication.
        states = {record["document_id"]: record for record in store.list_documents()}
        records = {}
        for document_id, state in states.items():
            record = _publication(state)
            if record is not None:
                records[document_id] = record
        inventory = api.inventory()
        if any(doc.get("document_metadata") is not None and not isinstance(doc["document_metadata"], dict)
               for doc in inventory):
            raise Error("invalid bank document metadata")
        bank_map = {doc["id"]: doc.get("document_metadata") or {} for doc in inventory}
        print(f"bank inventory: {len(bank_map)} document(s)")
        bank_hashes = {document_id: metadata.get("content_hash") or "" for document_id, metadata in bank_map.items()}
        staged = {i for i, state in states.items() if i not in records
                  and (state.get("publication_pending") or state.get("abandoned_attempt"))}
        decisions, _ = publication.plan(candidates, records, bank_hashes, accepted_ids, staged)
        for document_id, decision in sorted(decisions.items()):
            display = items[document_id][3]
            if decision.action == "hold":
                counts["held"] += 1
                finding(document_id, "held", decision.reason, display)
                record = records.get(document_id)
                if record and not record.get("visible", True):
                    counts["failed"] += 1
                    finding(document_id, "failed", "this build report is withdrawn from the bank and held; the "
                            "evidence is not visible until an eligible report revision publishes", display)
                elif record and bank_hashes.get(document_id) != record.get("source_hash"):
                    # A failed or interrupted retain left other bytes in the bank.
                    counts["failed"] += 1
                    finding(document_id, "failed", "the bank does not hold this document's published revision "
                            f"({record.get('relpath')} at {record.get('commit')}); an earlier retain failed or "
                            "is unresolved. Republish the published content or an eligible revision", display)
            elif decision.action == "refuse":
                counts["refused"] += 1
                finding(document_id, "refused", decision.reason, display)

        withdrawn = {i for i, r in records.items() if not r.get("visible", True)}
        claimed = set()

        def claim_namespace(repository):
            """Claim a namespace once a publication from it is confirmed."""
            namespace = namespaces.get(repository) or {}
            if not args.dry_run and namespace.get("claim") and repository not in claimed:
                store.put("namespace", namespace["key"], dict(namespace["claim"], claimed_at=now()))
                claimed.add(repository)

        def publish(document_id, set_id):
            item, source_hash, source, display = items[document_id]
            candidate = candidates[document_id]
            namespace = namespaces[candidate["repository"]]
            bank_hash = None if document_id in withdrawn else bank_map.get(document_id, {}).get("content_hash")
            previous = recovered.get(document_id, {})
            resumed_reprocess = (previous.get("reprocess") is True
                                 and previous.get("source_hash") == source_hash
                                 and previous.get("payload_hash") == _payload_hash(item))
            staged = decisions[document_id].action == "publish"
            planned = (publication.publication_record(candidate, records.get(document_id), set_id, now(),
                                                      document_id in accepted_ids) if staged else None)
            previous_attempt = (store.get("document", document_id) or {}).get("attempt") or {}
            if previous_attempt.get("source_hash") not in (None, source_hash):
                # Settle an earlier revision's operation without retrying it.
                try:
                    ingestor.recover(document_id, retry=False, replay=False)
                except Error as error:
                    if error.code != 1:
                        raise
            if ingestor.abandon_failed(document_id, source_hash, planned):
                finding(document_id, "abandoned", "a failed attempt for a revision this plan no longer "
                        "selects was retired instead of retried", display)
            elif staged:
                state = store.get("document", document_id) or {"document_id": document_id}
                state["publication_pending"] = planned
                store.put("document", document_id, state)
            outcome = ingestor.retain(item, source_hash, source, bank_hash,
                                      force=args.reprocess and not resumed_reprocess)
            if staged:
                if not promote(store, document_id):
                    raise Error(f"{document_id}: retain completed but its publication record did not advance")
                records[document_id] = store.get("document", document_id)["publication"]
                withdrawn.discard(document_id)
                claim_namespace(candidate["repository"])
            counts["skipped" if outcome == "unchanged" else "shipped"] += 1
            finding(document_id, outcome, display + (f" (set {set_id})" if set_id else ""), display)

        def failed(document_id, error):
            counts["failed"] += 1
            finding(document_id, "failed", str(error), items[document_id][3] if document_id in items else "")

        # A report's references were verified against Git, not bank visibility.
        # Each document publishes independently, including after partial failures.
        order = sorted(decisions.items(), key=lambda pair: (candidates[pair[0]]["type"] == publication.REPORT, pair[0]))
        for document_id, decision in order:
            if decision.action not in ("publish", "unchanged") or decision.set_id:
                continue
            if args.dry_run:
                state = store.get("document", document_id) or {}
                receipt = state.get("last_success") or {}
                item, source_hash = items[document_id][:2]
                unchanged = (decision.action == "unchanged" and not args.reprocess
                             and state.get("attempt", {}).get("state") == "succeeded"
                             and receipt.get("payload_hash") == _payload_hash(item))
                counts["skipped" if unchanged else "shipped"] += 1
                finding(document_id, "unchanged" if unchanged else "would ship", items[document_id][3])
                continue
            try:
                publish(document_id, None)
            except Error as error:
                failed(document_id, error)
                if error.code != 1:
                    # An unavailable store or unknown operation cannot be treated
                    # as permission to start more work on the same bank.
                    raise

        # A crash after a confirmed publication but before its claim leaves the
        # claim to the next scan that sees the record.
        for document_id, candidate in candidates.items():
            if (records.get(document_id) or {}).get("namespace") == (namespaces.get(candidate["repository"]) or {}).get("key"):
                claim_namespace(candidate["repository"])
        if not args.dry_run:
            # Old coupled sets are audit history. Per-document attempts still
            # recover normally, but no set can trigger another withdrawal.
            for set_state in store.list_records("set"):
                if set_state.get("state") in ("published", "replaced") or not set_state.get("members"):
                    continue
                if full:
                    set_state.update(state="replaced", replaced_at=now(),
                                     reason="intent and historical assessments publish independently")
                    store.put("set", set_state["document_id"], set_state)

        # A legacy document has only a repo basename. Do not attribute it when
        # multiple scanned origins have the same name.
        repo_origins = {}
        for root in result["roots"]:
            repo_origins.setdefault(root["repo"], set()).add(root["repository"])
        for document_id, metadata in bank_map.items():
            if document_id in seen or not metadata.get("repo"):
                continue
            for root in result["roots"]:
                same_repo = (metadata.get("repository") == root["repository"] if metadata.get("repository")
                             else metadata["repo"] == root["repo"] and len(repo_origins[root["repo"]]) == 1)
                path = metadata.get("relpath", "")
                if same_repo and (not root["prefix"] or path.startswith(root["prefix"] + "/")):
                    counts["gone"] += 1
                    finding(document_id, "gone", "missing from the fetched source; nothing deleted", path)
                    break
        code = 1 if counts["failed"] or counts["refused"] else 0
    except (Error, ValueError, OSError) as error:
        code = getattr(error, "code", 5)
        print(f"ship: {error}", file=sys.stderr)
        if report is not None:
            report.run["error"] = str(error)
            if not report.run["roots"]:
                report.run["roots"] = [dict(root="", status="failed", detail=str(error))]
                report.run["counts"]["incomplete"] = 1
    finally:
        try:
            if report is not None:
                print("---")
                print(" ".join(f"{key}={value}" for key, value in report.run["counts"].items()))
            if started:
                try:
                    print(f"scan record: {report.finish(code)}")
                except Error as error:
                    print(f"ship report: {error}", file=sys.stderr)
                    code = 5
        finally:
            resources.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
