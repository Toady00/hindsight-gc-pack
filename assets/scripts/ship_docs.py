"""Git-only document shipping with durable, completion-aware Beads receipts."""
import argparse
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
from ingestion import API, BeadsStore, Error, Ingestor, _payload_hash, require_writer
from ship_report import ScanReport

PACK = Path(__file__).resolve().parents[2]


def discover_roots(city):
    try:
        result = subprocess.run([os.environ.get("GC_BIN") or "gc", "rig", "list", "--json", "--city", city],
                                capture_output=True, text=True, timeout=120, check=True)
        registry = json.loads(result.stdout)
        rigs = registry["rigs"]
        if not isinstance(rigs, list) or any(not isinstance(r, dict) or not isinstance(r.get("name"), str)
                                           or not isinstance(r.get("path"), str) or not r["path"] for r in rigs):
            raise ValueError("invalid rigs")
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        raise Error("cannot discover rig docs roots from the city registry") from None
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
        if len(statuses) != 1 or statuses[0] not in {"status:" + s for s in ("draft", "accepted", "superseded", "deprecated")}:
            return dict(verdict="refuse", document_id=verdict["document_id"],
                        reason="schema must emit exactly one valid status tag")
        return verdict
    except subprocess.CalledProcessError:
        raise Error("schema derive failed", 1) from None
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise Error(f"schema derive failed: {error}", 1) from None


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
    return item, source_hash, source


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
        ingestor = Ingestor(store, api)
        full = args.full_scan or not args.roots
        report = ScanReport(store, full)
        counts = dict(shipped=0, skipped=0, refused=0, failed=0, gone=0, incomplete=0, recovered=0)
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
        roots = args.roots or discover_roots(store.city)
        if not roots:
            raise Error("no docs roots configured; add a Git rig or pass a Git docs root")
        result = snapshot(roots, args.ref)
        report.run["roots"] = result["roots"]
        counts["incomplete"] = result["errors"]
        for root in result["roots"]:
            print(f"root {root['status']}: {root['root']} {root['ref']} {root['commit']} {root['detail']}")
        if result["errors"]:
            raise Error("incomplete Git snapshot; fix repository, origin, branch, or traversal errors; nothing retained")
        executable = Path(args.schema).resolve() / "derive"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise Error("schema has no executable derive", 2)

        # Validate the entire manifest before submitting anything. A duplicate ID
        # refuses both files, rather than retaining whichever happens to come first.
        candidates, seen, duplicates = {}, set(), set()
        for document in result["documents"]:
            display = f"{document['repo']}@{document['commit']}:{document['relpath']}"
            try:
                verdict = derive(document, executable)
                document_id = verdict.get("document_id")
                if document_id and document_id != "?":
                    if document_id in seen:
                        duplicates.add(document_id)
                    seen.add(document_id)
                if verdict["verdict"] == "skip":
                    continue
                if verdict["verdict"] == "refuse":
                    counts["refused"] += 1
                    finding(document_id or "?", "refused", verdict.get("reason", "schema refused"), display)
                    continue
                candidates[document_id] = (*item_from(document, verdict), display)
            except Error as error:
                counts["failed"] += 1
                finding("?", "failed", str(error), display)
        for document_id in duplicates:
            candidates.pop(document_id, None)
            counts["refused"] += 1
            finding(document_id, "refused", "multiple published files claim this document ID")

        if not args.dry_run:
            api.drain(args.drain_timeout)
        # Recover even records whose source moved or disappeared. Pending payloads
        # live in Beads, so a handoff does not need the previous machine's checkout.
        recovered = {}
        records = store.list_documents()
        if not args.dry_run:
            for record in records:
                if ingestor.recover(record["document_id"]):
                    recovered[record["document_id"]] = record.get("attempt") or {}
                    counts["recovered"] += 1
                    finding(record["document_id"], "recovered", "prior operation completed and receipt confirmed")
        inventory = api.inventory()
        if any(doc.get("document_metadata") is not None and not isinstance(doc["document_metadata"], dict)
               for doc in inventory):
            raise Error("invalid bank document metadata")
        bank_map = {doc["id"]: doc.get("document_metadata") or {} for doc in inventory}
        print(f"bank inventory: {len(bank_map)} document(s)")
        for document_id, (item, source_hash, source, display) in candidates.items():
            try:
                bank_hash = bank_map.get(document_id, {}).get("content_hash")
                if args.dry_run:
                    state = store.get("document", document_id) or {}
                    receipt = state.get("last_success") or {}
                    unchanged = (not args.reprocess and state.get("attempt", {}).get("state") == "succeeded"
                                 and receipt.get("source_hash") == bank_hash == source_hash
                                 and receipt.get("payload_hash") == _payload_hash(item))
                    outcome = "unchanged" if unchanged else "would ship"
                else:
                    previous = recovered.get(document_id, {})
                    resumed_reprocess = (previous.get("reprocess") is True
                                         and previous.get("source_hash") == source_hash
                                         and previous.get("payload_hash") == _payload_hash(item))
                    outcome = ingestor.retain(item, source_hash, source, bank_hash,
                                              force=args.reprocess and not resumed_reprocess)
                counts["skipped" if outcome == "unchanged" else "shipped"] += 1
                finding(document_id, outcome, display, display)
            except Error as error:
                counts["failed"] += 1
                finding(document_id, "failed", str(error), display)
                if error.code != 1:
                    # An unavailable store or unknown operation cannot be treated
                    # as permission to start more work on the same bank.
                    raise

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
        if report is not None:
            print("---")
            print(" ".join(f"{key}={value}" for key, value in report.run["counts"].items()))
        if started:
            try:
                print(f"scan record: {report.finish(code)}")
            except Error as error:
                print(f"ship report: {error}", file=sys.stderr)
                code = 5
    return code


if __name__ == "__main__":
    sys.exit(main())
