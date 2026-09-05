"""Read published Markdown from freshly fetched origin branches, never a checkout.

``relpath`` is repository-relative. Overlapping roots share one document, owned
by the first successful root in input order. Only an ``ok`` root is a complete
inventory suitable for detecting removed documents, even if its directory is
absent from the published tree. A submodule boundary, Markdown symlink, or
invalid UTF-8 fails that inventory.
"""

import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


def _git(repo, *args):
    env = os.environ.copy()
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE", "GIT_SHALLOW_FILE",
    ):
        env.pop(name, None)
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"git {args[0]} timed out") from None
    except OSError:
        raise ValueError(f"git {args[0]} could not run") from None
    if result.returncode:
        # Git errors can echo credential-bearing URLs. Never include stderr.
        raise ValueError(f"git {args[0]} failed")
    return result.stdout


def _origin(repo):
    remote = _git(repo, "remote", "get-url", "origin").decode("utf-8").rstrip("\n")
    if not remote or any(c in remote for c in "\r\n\0"):
        raise ValueError("invalid origin URL")
    if "://" in remote:
        try:
            url = urlsplit(remote)
            if (
                url.scheme not in ("http", "https", "ssh", "git", "file")
                or url.query or url.fragment or url.password is not None
                or (url.scheme in ("http", "https") and url.username is not None)
                or (url.scheme != "file" and not url.hostname)
            ):
                raise ValueError
            # SSH login names are not part of the repository identity.
            identity = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1],
                                   url.path.rstrip("/"), "", ""))
            path = url.path
        except ValueError:
            raise ValueError("origin URL must not contain credentials, query, or fragment") from None
    elif re.match(r"^(?:[^/@:]+@)?[^/:]+:", remote):
        host, path = remote.split(":", 1)
        if "?" in remote or "#" in remote or not path or path.startswith(":"):
            raise ValueError("invalid origin URL")
        identity = host.rsplit("@", 1)[-1] + ":" + path.rstrip("/")
    else:
        path = str((repo / remote).resolve())
        identity = Path(path).as_uri()
    name = Path(path.rstrip("/")).name.removesuffix(".git")
    if not name:
        raise ValueError("origin URL has no repository name")
    return name, identity


def _branch(repo, ref):
    if not ref:
        advertisement = _git(repo, "ls-remote", "--symref", "origin", "HEAD").decode("utf-8")
        targets = [line[5:-5] for line in advertisement.splitlines()
                   if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD")]
        if len(targets) != 1:
            raise ValueError("origin HEAD does not identify a default branch")
        ref = targets[0]
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    if not ref or ref == "HEAD" or ref.startswith("refs/") or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref):
        raise ValueError("ref must name a remote branch, not HEAD, a tag, or a commit")
    branch = "refs/heads/" + ref
    _git(repo, "check-ref-format", branch)
    return branch


def _documents(repo, report):
    tree = _git(repo, "ls-tree", "-r", "-t", "-z", "--full-tree", report["commit"])
    if tree and not tree.endswith(b"\0"):
        raise ValueError("incomplete git ls-tree output")
    prefix = report["prefix"]
    entries = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ")
        path = raw_path.decode("utf-8")
        if mode not in ("040000", "100644", "100755", "120000", "160000"):
            raise ValueError("unsupported published tree mode")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise ValueError("invalid published object ID")
        at_or_under = not prefix or path == prefix or path.startswith(prefix + "/")
        ancestor = prefix.startswith(path + "/")
        if (at_or_under or ancestor) and mode == "160000":
            raise ValueError("published root contains or crosses a submodule boundary")
        if ancestor and mode == "120000":
            raise ValueError("published root crosses a symlink")
        if not at_or_under:
            continue
        if mode == "120000" and (path.lower().endswith(".md") or path == prefix):
            raise ValueError("published Markdown or root is a symlink")
        if path.lower().endswith(".md") and kind != "tree":
            if kind != "blob" or mode not in ("100644", "100755"):
                raise ValueError("published Markdown is not a regular blob")
            entries.append((path, oid))
    documents = []
    for path, oid in entries:
        content = _git(repo, "cat-file", "blob", oid).decode("utf-8")
        documents.append({
            key: report[key] for key in ("root", "repo", "repository", "ref", "commit")
        } | {"relpath": path, "content": content})
    return documents


def snapshot(roots: list[str], ref: str = "") -> dict:
    """Return ``roots``, deduplicated ``documents``, and failed-root ``errors``.

    Each common repository is fetched once per call into a unique temporary
    Git ref. It stays reachable until every associated root has been scanned,
    then is deleted. Neither FETCH_HEAD nor local/remote-tracking refs are used
    as the source of a commit. No checkout, journal, or other file is written.
    """
    reports = []
    groups = {}
    candidates = {}
    for root in roots:
        report = dict(root=str(Path(root).absolute()), repo="", repository="", prefix="",
                      ref="", commit="", status="failed", detail="")
        reports.append(report)
        try:
            physical = Path(root).resolve()
            report["root"] = str(physical)
            ancestor = physical
            while not ancestor.exists():
                if ancestor == ancestor.parent:
                    raise ValueError("root has no existing ancestor")
                ancestor = ancestor.parent
            if not ancestor.is_dir():
                ancestor = ancestor.parent
            repo = Path(_git(ancestor, "rev-parse", "--show-toplevel").decode("utf-8").rstrip("\n")).resolve()
            report["prefix"] = physical.relative_to(repo).as_posix()
            if report["prefix"] == ".":
                report["prefix"] = ""
            if _git(repo, "rev-parse", "--show-superproject-working-tree").strip():
                raise ValueError("submodule roots are not supported")
            common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode("utf-8").rstrip("\n")).resolve()
            groups.setdefault(common, []).append((repo, report))
        except (ValueError, OSError, RuntimeError) as error:
            report["detail"] = ("cannot resolve or contain root" if isinstance(error, (OSError, RuntimeError))
                                else str(error))

    for members in groups.values():
        repo = members[0][0]
        private_ref = "refs/hindsight-snapshot/" + uuid4().hex
        fetch_attempted = False
        try:
            name, identity = _origin(repo)
            for _, report in members:
                report.update(repo=name, repository=identity)
            branch = _branch(repo, ref)
            for _, report in members:
                report["ref"] = branch
            # An explicit destination and empty refmap prevent configured fetch
            # mappings from updating shared refs. FETCH_HEAD is never written.
            fetch_attempted = True
            _git(repo, "fetch", "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance",
                 "--no-write-fetch-head", "--refmap=", "origin", f"+{branch}:{private_ref}")
            commit = _git(repo, "rev-parse", "--verify", private_ref + "^{commit}").decode("ascii").strip()
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
                raise ValueError("fetch did not resolve a commit")
            for root_repo, report in members:
                report["commit"] = commit
                try:
                    documents = _documents(root_repo, report)
                    candidates[id(report)] = documents
                    report.update(status="ok", detail=f"read {len(documents)} published Markdown documents")
                except (ValueError, OSError) as error:
                    report["detail"] = str(error)
        except (ValueError, OSError) as error:
            for _, report in members:
                report["detail"] = str(error)
        finally:
            if fetch_attempted:
                try:
                    _git(repo, "update-ref", "-d", private_ref)
                except ValueError as error:
                    for _, report in members:
                        report.update(status="failed", detail=str(error))

    documents = []
    seen = set()
    for report in reports:
        if report["status"] != "ok":
            continue
        for document in candidates.get(id(report), []):
            key = (document["repository"], document["commit"], document["relpath"])
            if key not in seen:
                seen.add(key)
                documents.append(document)
    return dict(roots=reports, documents=documents,
                errors=sum(report["status"] == "failed" for report in reports))
