"""Read-only document check for one repository (`gc hindsight check`).

Checks what one checkout can establish by itself: schema validity, the
repository namespace, ID namespace and uniqueness (retired documents still own
their IDs), and build-report pins. It reads the working tree, or a commit with
--rev; it never reads the Git index, writes files, or contacts the bank.

Publication history (acceptance and frozen revisions) lives in
the city's publication records. The publisher runs these same checks, then
enforces that history; a document passing here can still be held there.
"""
import argparse
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
import publication
from git_snapshot import _git
from ship_docs import PACK, derive
from ingestion import Error
from revision_history import RevisionHistory

GUIDANCE = f"""
Repair: give every published document an ID of the form <namespace>.<document-id>,
where <namespace> is the single line in {publication.NAMESPACE_FILE} at the
repository root (the city or rig name, lowercased, fixed after first publication).
Keep IDs unique, including retired documents. Build reports pin only documents of
their own repository by the fingerprint printed by `check --fingerprint`.
"""


def worktree_files(repo, prefixes):
    """Tracked and untracked, non-ignored Markdown in the working tree."""
    listing = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                   *[p or "." for p in prefixes])
    for raw in sorted(set(listing.split(b"\0"))):
        relpath = raw.decode("utf-8")
        path = repo / relpath
        if not relpath.lower().endswith(".md") or not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{relpath}: Markdown documents must be regular files")
        yield relpath, path.read_bytes()


def revision_files(repo, rev, prefixes):
    for record in _git(repo, "ls-tree", "-r", "-z", rev, "--", *[p or "." for p in prefixes]).split(b"\0"):
        if not record:
            continue
        header, raw = record.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ")
        path = raw.decode("utf-8")
        if not path.lower().endswith(".md") or kind != "blob":
            continue
        if mode not in ("100644", "100755"):
            raise ValueError(f"{path}: Markdown documents must be regular files")
        yield path, _git(repo, "cat-file", "blob", oid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rev", default="", help="check a commit instead of the working tree")
    parser.add_argument("--schema", default=os.environ.get("HINDSIGHT_SCHEMA") or str(PACK / "schemas/docs"))
    parser.add_argument("--fingerprint", action="store_true",
                        help="print each shippable document's lifecycle fingerprint, ID and path")
    parser.add_argument("paths", nargs="*", help="files or directories inside one repository (default: docs)")
    args = parser.parse_args(argv)
    try:
        start = Path(args.paths[0]).resolve() if args.paths else Path.cwd()
        anchor = start if start.is_dir() else start.parent
        repo = Path(_git(anchor, "rev-parse", "--show-toplevel").decode().rstrip("\n")).resolve()
        if args.rev:
            args.rev = _git(repo, "rev-parse", "--verify", "--end-of-options",
                            args.rev + "^{commit}").decode("ascii").strip()
        prefixes = []
        for path in args.paths or [str(repo / "docs")]:
            relative = Path(path).resolve().relative_to(repo).as_posix()
            prefixes.append("" if relative == "." else relative)
        executable = Path(args.schema).resolve() / "derive"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise Error("schema has no executable derive", 2)
        errors, documents, namespace = [], [], None
        try:
            if args.rev:
                listing = _git(repo, "ls-tree", "--name-only", args.rev, "--", publication.NAMESPACE_FILE)
                text = _git(repo, "show", f"{args.rev}:{publication.NAMESPACE_FILE}").decode() if listing else None
            else:
                file = repo / publication.NAMESPACE_FILE
                text = file.read_text() if file.is_file() else None
            if text is None:
                errors.append((publication.NAMESPACE_FILE, "missing; add one line naming this repository's "
                               "city or rig namespace"))
            else:
                namespace = publication.read_namespace(text)
        except ValueError as error:
            errors.append((publication.NAMESPACE_FILE, str(error)))
        # Uniqueness and report pins cover the repository's docs/ tree, which
        # the shipper scans, plus the selection; findings print for the selection.
        selected = lambda relpath: any(not p or relpath == p or relpath.startswith(p + "/") for p in prefixes)
        universe = sorted(set(prefixes) | {"docs"})
        files = revision_files(repo, args.rev, universe) if args.rev else worktree_files(repo, universe)
        for relpath, content in files:
            try:
                verdict = derive(dict(content=content.decode("utf-8")), executable)
            except (Error, UnicodeError) as error:
                if selected(relpath):
                    errors.append((relpath, f"schema derive failed: {error}"))
                continue
            documents.append(dict(relpath=relpath, verdict=verdict))
    except (ValueError, OSError, Error) as error:
        print(f"check: {error}", file=sys.stderr)
        return getattr(error, "code", 2) if isinstance(error, Error) else 2
    history = RevisionHistory(repo, args.rev or "HEAD", derive, executable)
    found, warnings = publication.check_repository(namespace, documents, history.lookup, history.is_legacy)
    errors += [(r, m) for r, m in found if selected(r)]
    warnings = [(r, m) for r, m in warnings if selected(r)]
    documents = [d for d in documents if selected(d["relpath"])]
    if args.fingerprint:
        for document in documents:
            verdict = document["verdict"]
            if verdict.get("verdict") == "ship":
                lifecycle = verdict.get("lifecycle") or {}
                print(f"{lifecycle.get('fingerprint', '?')}  {verdict['document_id']}  {document['relpath']}")
    for relpath, message in warnings:
        print(f"WARN  {relpath}: {message}", file=sys.stderr)
    for relpath, message in errors:
        print(f"ERROR {relpath}: {message}", file=sys.stderr)
    shippable = sum(d["verdict"].get("verdict") == "ship" for d in documents)
    print(f"checked {len(documents)} Markdown file(s), {shippable} shippable, namespace "
          f"{namespace or 'missing'}: {len(errors)} error(s), {len(warnings)} warning(s)", file=sys.stderr)
    if errors:
        print(GUIDANCE.rstrip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
