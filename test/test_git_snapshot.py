"""Offline publication tests using disposable local bare origins."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "git_snapshot", Path(__file__).resolve().parents[1] / "assets/scripts/git_snapshot.py"
)
git_snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_snapshot)


class GitSnapshotTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="git-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "0",
            "GIT_AUTHOR_NAME": "Snapshot Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Snapshot Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_ALLOW_PROTOCOL": "file",
        })
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            os.environ.pop(key, None)
        self.remote = self.base / "published.git"
        self.repo = self.base / "checkout"
        self.git(self.base, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.base, "init", "--initial-branch=main", str(self.repo))
        self.git(self.repo, "remote", "add", "origin", str(self.remote))
        self.write("docs/a.md", "---\ntitle: Published\n---\nWhole body.\r\n")
        self.write("docs/b.md", "Second document\n")
        self.write("plain/readme.txt", "Not Markdown\n")
        self.commit("published")
        self.git(self.repo, "push", "origin", "main")
        self.published = self.git(self.repo, "rev-parse", "HEAD").strip()

    def git(self, cwd, *args):
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(cwd), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return result.stdout.decode("utf-8")

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))

    def commit(self, message):
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "--allow-empty", "-m", message)

    def scan(self, *roots, ref=""):
        return git_snapshot.snapshot([str(root) for root in roots] or [str(self.repo / "docs")], ref)

    def assert_failed(self, result, count=1):
        self.assertEqual(result["errors"], count, result)
        self.assertEqual(result["documents"], [])
        for root in result["roots"]:
            self.assertEqual(root["status"], "failed")
            self.assertTrue(root["detail"])

    def test_canonical_branch_ignores_private_commits_dirty_and_untracked(self):
        self.git(self.repo, "switch", "-c", "private")
        self.write("docs/a.md", "Private commit\n")
        self.commit("private")
        self.write("docs/b.md", "Dirty\n")
        self.write("docs/untracked.md", "Untracked\n")
        before = self.git(self.repo, "status", "--porcelain")
        result = self.scan()
        self.assertEqual(result["errors"], 0)
        self.assertEqual([doc["relpath"] for doc in result["documents"]], ["docs/a.md", "docs/b.md"])
        self.assertEqual(result["documents"][0]["content"], "---\ntitle: Published\n---\nWhole body.\r\n")
        self.assertEqual(result["roots"][0], {
            "root": str(self.repo / "docs"), "repo": "published",
            "repository": self.remote.as_uri(), "prefix": "docs", "ref": "refs/heads/main",
            "commit": self.published, "status": "ok", "detail": "read 2 published Markdown documents",
        })
        self.assertEqual(before, self.git(self.repo, "status", "--porcelain"))
        self.assertFalse((self.repo / ".git/FETCH_HEAD").exists())
        self.assertEqual(self.git(self.repo, "for-each-ref", "refs/hindsight-snapshot/"), "")

    def test_missing_local_origin_head_is_irrelevant(self):
        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/HEAD")
        self.assertEqual(self.scan()["roots"][0]["commit"], self.published)

    def test_remote_default_change_overrides_stale_local_origin_head(self):
        self.git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git(self.repo, "switch", "-c", "release")
        self.write("docs/a.md", "New default\n")
        self.commit("release")
        self.git(self.repo, "push", "origin", "release")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/release")
        self.git(self.repo, "switch", "main")
        result = self.scan()
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["roots"][0]["ref"], "refs/heads/release")
        self.assertEqual(result["documents"][0]["content"], "New default\n")

    def test_explicit_remote_branch_spellings(self):
        self.git(self.repo, "switch", "-c", "release/docs")
        self.write("docs/a.md", "Explicit\n")
        self.commit("release")
        self.git(self.repo, "push", "origin", "release/docs")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/missing")
        for ref in ("release/docs", "origin/release/docs", "refs/heads/release/docs",
                    "refs/remotes/origin/release/docs"):
            with self.subTest(ref=ref):
                result = self.scan(ref=ref)
                self.assertEqual(result["errors"], 0)
                self.assertEqual(result["roots"][0]["ref"], "refs/heads/release/docs")
                self.assertEqual(result["documents"][0]["content"], "Explicit\n")

    def test_rejects_local_only_refs_commits_tags_and_invalid_branches(self):
        self.git(self.repo, "branch", "private")
        self.git(self.repo, "tag", "v1")
        self.git(self.repo, "push", "origin", "refs/tags/v1")
        for ref in ("private", self.published, "HEAD", "refs/tags/v1", "v1",
                    "refs/remotes/elsewhere/main", "main~1", "bad..branch", "--all"):
            with self.subTest(ref=ref):
                self.assert_failed(self.scan(ref=ref))

    def test_missing_origin_and_non_git_fail(self):
        self.git(self.repo, "remote", "remove", "origin")
        self.assert_failed(self.scan())
        self.assert_failed(self.scan(self.base / "not-a-repository/docs"))

    def test_missing_remote_default_fails_instead_of_using_local_head(self):
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/missing")
        self.assert_failed(self.scan())

    def test_published_root_absent_from_private_checkout(self):
        self.git(self.repo, "switch", "-c", "private")
        self.git(self.repo, "rm", "-r", "docs")
        self.commit("no local docs")
        self.assertFalse((self.repo / "docs").exists())
        result = self.scan()
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(result["documents"]), 2)

    def test_empty_published_tree_and_non_markdown_root_are_ok(self):
        result = self.scan(self.repo / "plain")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["documents"], [])
        self.git(self.repo, "rm", "-r", ".")
        self.commit("empty tree")
        self.git(self.repo, "push", "origin", "main")
        result = self.scan(self.repo)
        self.assertEqual(result["roots"][0]["status"], "ok")
        self.assertEqual(result["documents"], [])

    def test_missing_published_root_has_a_complete_empty_inventory(self):
        result = self.scan(self.repo / "missing")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["roots"][0]["status"], "ok")
        self.assertEqual(result["documents"], [])

    def test_fetch_listing_and_blob_failures_drop_partial_documents(self):
        real_run = subprocess.run
        for operation in ("fetch", "ls-tree", "cat-file"):
            calls = []

            def failing_run(command, **kwargs):
                if operation in command:
                    calls.append(command)
                    if operation != "cat-file" or len(calls) == 2:
                        return subprocess.CompletedProcess(command, 1, b"", b"secret remote credentials")
                return real_run(command, **kwargs)

            with self.subTest(operation=operation), patch.object(git_snapshot.subprocess, "run", side_effect=failing_run):
                result = self.scan()
                self.assert_failed(result)
                self.assertNotIn("secret", str(result))
            self.assertEqual(self.git(self.repo, "for-each-ref", "refs/hindsight-snapshot/"), "")

    def test_failed_root_does_not_discard_other_successful_roots(self):
        real_run = subprocess.run
        calls = 0

        def failing_run(command, **kwargs):
            nonlocal calls
            if "cat-file" in command:
                calls += 1
                if calls == 2:
                    return subprocess.CompletedProcess(command, 1, b"", b"failed")
            return real_run(command, **kwargs)

        with patch.object(git_snapshot.subprocess, "run", side_effect=failing_run):
            result = self.scan(self.repo / "docs", self.repo)
        self.assertEqual(result["errors"], 1)
        self.assertEqual([root["status"] for root in result["roots"]], ["failed", "ok"])
        self.assertEqual(len(result["documents"]), 2)
        self.assertTrue(all(doc["root"] == str(self.repo) for doc in result["documents"]))

    def test_unicode_spaces_tabs_newlines_and_literal_prefix(self):
        path = "docs/[draft]*/caf\u00e9 space\tline\n.md"
        self.write(path, "Entire UTF-8: \u96ea\n")
        self.write("docs/draft-other/no.md", "Not under the literal prefix\n")
        self.commit("unusual names")
        self.git(self.repo, "push", "origin", "main")
        result = self.scan(self.repo / "docs/[draft]*")
        self.assertEqual(result["errors"], 0)
        self.assertEqual([doc["relpath"] for doc in result["documents"]], [path])
        self.assertEqual(result["documents"][0]["content"], "Entire UTF-8: \u96ea\n")

    def test_worktrees_overlap_share_one_fetch_and_pinned_commit(self):
        worktree = self.base / "worktree"
        self.git(self.repo, "worktree", "add", "-b", "private", str(worktree))
        real_run = subprocess.run
        with patch.object(git_snapshot.subprocess, "run", wraps=real_run) as run:
            result = self.scan(self.repo / "docs", worktree / "docs", self.repo)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(result["roots"]), 3)
        self.assertEqual(len(result["documents"]), 2)
        self.assertEqual(sum("fetch" in call.args[0] for call in run.call_args_list), 1)
        self.assertEqual(sum("ls-remote" in call.args[0] for call in run.call_args_list), 1)
        self.assertEqual({root["commit"] for root in result["roots"]}, {self.published})
        self.assertTrue(all(doc["root"] == str(self.repo / "docs") for doc in result["documents"]))

    def test_dedup_uses_remote_identity_not_basename_or_content(self):
        other = self.base / "other"
        other.mkdir()
        other_remote = other / self.remote.name
        other_checkout = other / "checkout"
        self.git(self.base, "clone", "--bare", str(self.remote), str(other_remote))
        self.git(self.base, "clone", str(other_remote), str(other_checkout))
        result = self.scan(self.repo / "docs", other_checkout / "docs")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(result["documents"]), 4)
        self.assertEqual({doc["repo"] for doc in result["documents"]}, {"published"})
        self.assertEqual({doc["repository"] for doc in result["documents"]},
                         {self.remote.as_uri(), other_remote.as_uri()})
        self.git(other_checkout, "remote", "set-url", "origin", str(self.remote))
        result = self.scan(self.repo / "docs", other_checkout / "docs")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(len(result["documents"]), 2)

    def test_branch_and_fetch_head_moves_do_not_change_snapshot(self):
        self.write("docs/a.md", "Later commit\n")
        self.write("docs/b.md", "Later second document\n")
        self.commit("later")
        later = self.git(self.repo, "rev-parse", "HEAD").strip()
        real_run = subprocess.run
        moved = False

        def moving_run(command, **kwargs):
            nonlocal moved
            if "ls-tree" in command and not moved:
                moved = True
                self.git(self.repo, "push", "origin", "main")
                self.git(self.repo, "fetch", "origin", "main")
            return real_run(command, **kwargs)

        with patch.object(git_snapshot.subprocess, "run", side_effect=moving_run):
            result = self.scan(self.repo / "docs", self.repo)
        self.assertTrue(moved)
        self.assertEqual(result["errors"], 0)
        self.assertEqual({root["commit"] for root in result["roots"]}, {self.published})
        self.assertEqual(result["documents"][1]["content"], "Second document\n")
        self.assertEqual(self.git(self.repo, "rev-parse", "FETCH_HEAD").strip(), later)
        self.assertEqual(self.scan()["roots"][0]["commit"], later)

    def test_interleaved_fetch_before_commit_resolution_uses_private_ref(self):
        self.write("docs/a.md", "Concurrent fetch\n")
        self.commit("later")
        later = self.git(self.repo, "rev-parse", "HEAD").strip()
        real_run = subprocess.run
        moved = False

        def moving_run(command, **kwargs):
            nonlocal moved
            result = real_run(command, **kwargs)
            if "fetch" in command and "--no-write-fetch-head" in command and not moved:
                moved = True
                self.git(self.repo, "push", "origin", "main")
                self.git(self.repo, "fetch", "origin", "main")
            return result

        with patch.object(git_snapshot.subprocess, "run", side_effect=moving_run):
            result = self.scan()
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["roots"][0]["commit"], self.published)
        self.assertNotEqual(result["documents"][0]["content"], "Concurrent fetch\n")
        self.assertEqual(self.git(self.repo, "rev-parse", "FETCH_HEAD").strip(), later)

    def test_local_replace_refs_do_not_override_published_objects(self):
        original = self.git(self.repo, "rev-parse", self.published + ":docs/a.md").strip()
        self.write("docs/a.md", "Local replacement\n")
        self.commit("replacement")
        replacement = self.git(self.repo, "rev-parse", "HEAD:docs/a.md").strip()
        self.git(self.repo, "replace", original, replacement)
        result = self.scan()
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["documents"][0]["content"], "---\ntitle: Published\n---\nWhole body.\r\n")

    def test_failed_fetch_is_not_retried_for_overlapping_roots(self):
        real_run = subprocess.run
        fetches = 0

        def failing_run(command, **kwargs):
            nonlocal fetches
            if "fetch" in command:
                fetches += 1
                return subprocess.CompletedProcess(command, 1, b"", b"failed")
            return real_run(command, **kwargs)

        with patch.object(git_snapshot.subprocess, "run", side_effect=failing_run):
            result = self.scan(self.repo / "docs", self.repo)
        self.assert_failed(result, count=2)
        self.assertEqual(fetches, 1)

    def test_incomplete_tree_output_fails(self):
        real_run = subprocess.run

        def truncated_run(command, **kwargs):
            result = real_run(command, **kwargs)
            if "ls-tree" in command:
                result.stdout = result.stdout[:-1]
            return result

        with patch.object(git_snapshot.subprocess, "run", side_effect=truncated_run):
            self.assert_failed(self.scan())

    def test_git_executable_failure_is_reported(self):
        with patch.object(git_snapshot.subprocess, "run", side_effect=OSError("secret")):
            result = self.scan()
        self.assert_failed(result)
        self.assertNotIn("secret", str(result))

    def test_private_ref_cleanup_failure_is_not_suppressed(self):
        real_run = subprocess.run

        def failing_run(command, **kwargs):
            if "update-ref" in command:
                return subprocess.CompletedProcess(command, 1, b"", b"failed")
            return real_run(command, **kwargs)

        with patch.object(git_snapshot.subprocess, "run", side_effect=failing_run):
            self.assert_failed(self.scan())

    def test_local_symlink_root_is_canonicalized_including_missing_suffix(self):
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.git(self.repo, "switch", "-c", "private")
        self.git(self.repo, "rm", "-r", "docs")
        self.commit("missing local docs")
        result = self.scan(alias / "docs")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["roots"][0]["root"], str(self.repo / "docs"))
        outside = self.base / "outside"
        outside.mkdir()
        (self.repo / "escape").symlink_to(outside, target_is_directory=True)
        self.assert_failed(self.scan(self.repo / "escape"))

    def test_published_markdown_symlink_fails_without_reading_target(self):
        (self.repo / "docs/link.md").symlink_to("a.md")
        self.commit("symlink")
        self.git(self.repo, "push", "origin", "main")
        self.assert_failed(self.scan())

    def test_submodule_boundary_fails_loudly(self):
        self.git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.published},modules/library")
        self.git(self.repo, "commit", "-m", "gitlink")
        self.git(self.repo, "push", "origin", "main")
        for root in (self.repo / "modules/library", self.repo / "modules/library/docs", self.repo):
            with self.subTest(root=root):
                result = self.scan(root)
                self.assert_failed(result)
                self.assertIn("submodule", result["roots"][0]["detail"])

    def test_invalid_utf8_is_a_failed_inventory(self):
        (self.repo / "docs/b.md").write_bytes(b"\xff")
        self.commit("invalid UTF-8")
        self.git(self.repo, "push", "origin", "main")
        self.assert_failed(self.scan())

    def test_credential_bearing_origins_are_rejected_without_exposure(self):
        for remote in ("https://user:secret@example.invalid/repo.git",
                       "https://secret@example.invalid/repo.git",
                       "https://example.invalid/repo.git?token=secret",
                       "ssh://user:secret@example.invalid/repo.git"):
            with self.subTest(remote=remote):
                self.git(self.repo, "remote", "set-url", "origin", remote)
                result = self.scan()
                self.assert_failed(result)
                self.assertNotIn("secret", str(result))
                self.assertEqual(result["roots"][0]["repository"], "")

    def test_origin_identity_omits_ssh_login(self):
        for remote, identity in (
            ("git@example.invalid:team/repo.git", "example.invalid:team/repo.git"),
            ("ssh://git@example.invalid/team/repo.git", "ssh://example.invalid/team/repo.git"),
            ("https://example.invalid/team/repo.git", "https://example.invalid/team/repo.git"),
        ):
            with self.subTest(remote=remote):
                self.git(self.repo, "remote", "set-url", "origin", remote)
                self.assertEqual(git_snapshot._origin(self.repo), ("repo", identity))

    def test_effective_origin_url_rewrite_is_checked_for_credentials(self):
        self.git(self.repo, "config", "url.https://user:secret@example.invalid/.insteadOf", "safe:")
        self.git(self.repo, "remote", "set-url", "origin", "safe:repo.git")
        result = self.scan()
        self.assert_failed(result)
        self.assertNotIn("secret", str(result))

    def test_empty_input(self):
        self.assertEqual(git_snapshot.snapshot([]), {"roots": [], "documents": [], "errors": 0})


if __name__ == "__main__":
    unittest.main()
