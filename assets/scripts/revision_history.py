"""Resolve assessed document revisions from the selected repository's Git history.

Only ancestors of the checked/fetched commit count. Markdown moves and deletions
are handled by reading changed blobs, never the current checkout or other refs.
"""
import hashlib
import importlib.util
from pathlib import Path
import re

from git_snapshot import _git
from ingestion import Error


class RevisionHistory:
    def __init__(self, repo, revision, derive, executable):
        self.repo, self.revision = repo, revision
        self.derive, self.executable = derive, executable
        self.revisions, self.blobs, self.failure = {}, None, None
        self.schema = None
        default = Path(__file__).resolve().parents[2] / "schemas/docs/derive"
        if Path(executable).resolve() == default:
            spec = importlib.util.spec_from_file_location("historical_docs_schema", default.with_name("validate.py"))
            self.schema = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.schema)

    def lookup(self, document_id, fingerprint):
        if self.failure:
            raise ValueError(self.failure)
        if self.blobs is None:
            try:
                self._load()
            except (ValueError, OSError) as error:
                self.failure = str(error)
                raise
        key = document_id, fingerprint
        if key not in self.revisions:
            kind = None
            for content in self.blobs.get(fingerprint, []):
                try:
                    if self.schema:
                        identity, typ = self.schema.historical_revision(content, self.schema._yq(content), fingerprint)
                        if identity == document_id:
                            kind = typ
                            break
                    else:
                        verdict = self.derive(dict(content=content), self.executable)
                        lifecycle = verdict.get("lifecycle") or {}
                        actual = lifecycle.get("fingerprint") or hashlib.sha256(content.encode()).hexdigest()
                        if verdict.get("verdict") == "ship" and verdict.get("document_id") == document_id and actual == fingerprint:
                            kind = lifecycle.get("type") or ""
                            break
                except (Error, ValueError, TypeError, OSError):
                    continue  # A malformed historical blob is not usable evidence.
            self.revisions[key] = kind
        if self.revisions[key] is None and self.shallow:
            raise ValueError("assessed revision is absent from shallow Git history; fetch full history")
        return self.revisions[key]

    def _load(self):
        self.revision = _git(self.repo, "rev-parse", "--verify", "--end-of-options",
                             self.revision + "^{commit}").decode("ascii").strip()
        self.shallow = _git(self.repo, "rev-parse", "--is-shallow-repository").strip() == b"true"
        # --root includes first versions; -m reads each merge parent's changes.
        # NUL-delimited raw records preserve whitespace and newlines in paths.
        output = _git(self.repo, "log", "--format=", "--raw", "-z", "--no-abbrev", "--full-history",
                      "--no-renames", "--root", "-m", self.revision, "--", ":(icase)*.md")
        tokens, index, blobs = output.split(b"\0"), 0, set()
        while index < len(tokens):
            header = tokens[index].lstrip(b"\n")
            index += 1
            if not header:
                continue
            if not header.startswith(b":") or index >= len(tokens):
                raise ValueError("cannot read document revision history")
            fields = header[1:].split()
            path = tokens[index]
            index += 1
            if len(fields) != 5 or not path.lower().endswith(b".md"):
                raise ValueError("invalid document revision history record")
            old_mode, new_mode, old_oid, new_oid, _ = fields
            for mode, oid in [(old_mode, old_oid), (new_mode, new_oid)]:
                if mode not in (b"100644", b"100755"):
                    continue
                if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", oid):
                    raise ValueError("invalid historical document blob ID")
                blobs.add(oid.decode("ascii"))
        ordered = sorted(blobs)
        batch = _git(self.repo, "cat-file", "--batch", data="".join(oid + "\n" for oid in ordered).encode())
        previews, offset = {}, 0
        for oid in ordered:
            end = batch.find(b"\n", offset)
            fields = batch[offset:end].split() if end >= 0 else []
            if len(fields) != 3 or fields[0].decode("ascii") != oid or fields[1] != b"blob" or not fields[2].isdigit():
                raise ValueError("invalid historical blob batch")
            size = int(fields[2])
            raw = batch[end + 1:end + 1 + size]
            offset = end + size + 2
            if len(raw) != size or batch[offset - 1:offset] != b"\n":
                raise ValueError("incomplete historical blob batch")
            try:
                content = raw.decode("utf-8")
            except UnicodeError:
                continue  # Invalid Markdown is not evidence for an assessed revision.
            hashes = {hashlib.sha256(raw).hexdigest()}
            if self.schema:
                # Cheap lexical previews only select candidates. Matching blobs
                # still undergo duplicate-rejecting YAML and mask equivalence checks.
                for fields in [("status", "updated_at"), ("updated_at",), ("status",)]:
                    try:
                        hashes.add(hashlib.sha256(self.schema.masked_text(content, fields).encode()).hexdigest())
                    except ValueError:
                        pass
            else:
                try:
                    verdict = self.derive(dict(content=content), self.executable)
                    if verdict.get("verdict") == "ship":
                        hashes.add((verdict.get("lifecycle") or {}).get("fingerprint") or hashlib.sha256(raw).hexdigest())
                except (Error, ValueError, TypeError, OSError):
                    continue
            for digest in hashes:
                previews.setdefault(digest, []).append(content)
        if offset != len(batch):
            raise ValueError("unexpected historical blob batch data")
        self.blobs = previews
