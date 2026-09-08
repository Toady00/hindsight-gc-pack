"""Survey lifecycle against real local Git; Beads and forge never leave the fixture."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    from .pack_fixture import SCRIPTS
except ImportError:
    from pack_fixture import SCRIPTS


MOCK = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ["MOCK_ROOT"])
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps([tool, args]) + "\n")
if tool == "gc":
    if args == ["rig", "list", "--json"]:
        if os.environ.get("FAIL_RIG_LIST"):
            print("rig registry unavailable", file=sys.stderr)
            sys.exit(1)
        print((root / "rigs.json").read_text())
        sys.exit(0)
    assert args[:3] == ["bd", "--rig", "repo"], args
    assert os.environ["GC_RIG"] == "repo"
    assert os.environ["GC_RIG_ROOT"] == str(root / "repo")
    assert os.environ["GC_CITY"] == str(root)
    args = [args[0], *args[3:]]
    state = root / "state.json"
    rows = json.loads(state.read_text())
    command, bead = args[1:3]
    if command == "show":
        metadata = {"gc.kind": "workflow", "gc.formula_name": "current-state-survey",
                    "gc.root_store_ref": "rig:repo"}
        metadata.update(rows.get(bead, {}))
        print(json.dumps({"id": os.environ.get("FAIL_ROOT_ID", bead), "metadata": metadata}))
    elif command == "list":
        assert args[2:] == ["--parent", args[3], "--status", "all", "--limit", "0", "--json"], args
        if os.environ.get("FAIL_SCOPE"):
            sys.exit(1)
        print(json.dumps(rows.get(args[3], {}).get("scopes", [])))
    elif command == "update":
        if os.environ.get("FAIL_METADATA") == "error":
            sys.exit(1)
        for arg in args[3:]:
            assert arg.startswith("--set-metadata="), arg
            key, value = arg[len("--set-metadata="):].split("=", 1)
            if os.environ.get("FAIL_METADATA") == key:
                continue
            rows.setdefault(bead, {})[key] = value
        state.write_text(json.dumps(rows))
    else:
        raise AssertionError(args)
else:
    if os.environ.get("FAIL_FORGE") == args[1]:
        sys.exit(1)
    pr = root / "pr-url"
    if args[1] == "list":
        url = pr.read_text() if pr.exists() else ""
        print(json.dumps([{"web_url": url}]) if tool == "glab" else url)
    elif args[1] == "create":
        if tool == "gh":
            body = sys.stdin.read()
        else:
            body = args[args.index("--description") + 1]
        (root / "pr-body").write_text(body)
        pr.write_text("https://forge.invalid/survey/1")
        print(pr.read_text())
    else:
        raise AssertionError(args)
'''


class SurveyTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hindsight-survey-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("gc", "gh", "glab"):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n" + MOCK)
            path.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GC_", "GIT_", "BEADS_", "BD_", "MOCK_", "FAIL_"))}
        self.repo = self.root / "repo"
        self.origin = self.root / "origin.git"
        self.env.update(PATH=f"{self.bin}:{Path(sys.executable).parent}:{os.environ['PATH']}",
                        HOME=str(self.root), MOCK_ROOT=str(self.root), GC_CITY=str(self.root),
                        GC_RIG="repo", GC_RIG_ROOT=str(self.repo),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
                        GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0",
                        GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
                        PYTHONDONTWRITEBYTECODE="1")
        (self.root / "state.json").write_text("{}")
        (self.root / "rigs.json").write_text(json.dumps({"city_path": str(self.root), "rigs": [
            {"name": "hq", "path": str(self.root), "hq": True},
            {"name": "repo", "path": str(self.repo), "hq": False}]}))
        self.git("init", "--bare", "-q", "-b", "main", str(self.origin), cwd=self.root)
        self.git("clone", "-q", str(self.origin), str(self.repo), cwd=self.root)
        (self.repo / "source.txt").write_text("source\n")
        self.commit(self.repo)
        self.git("push", "-q", "origin", "main")
        self.git("remote", "set-head", "origin", "-a")

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def commit(self, cwd):
        self.git("add", ".", cwd=cwd)
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture", cwd=cwd)

    def run_survey(self, command, *args, root="run-1", ok=True, cwd=None):
        result = subprocess.run([str(SCRIPTS / "survey.sh"), command, root, *args],
                                cwd=cwd or self.repo, env=self.env, text=True, capture_output=True, timeout=20)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def meta(self, root="run-1", **updates):
        path = self.root / "state.json"
        rows = json.loads(path.read_text())
        if updates:
            rows.setdefault(root, {}).update(updates)
            path.write_text(json.dumps(rows))
        return rows.get(root, {})

    def prepare(self, mode="none", tool="gh", root="run-1"):
        self.run_survey("prepare", "--publish", mode, "--pr-tool", tool, root=root)
        return Path(self.meta(root)["work_dir"])

    def scope_pass(self, root="run-1"):
        scope = {"id": f"{root}-scope", "status": "closed", "metadata": {
            "gc.root_bead_id": root, "gc.step_ref": "current-state-survey.worktree",
            "gc.kind": "scope", "gc.scope_role": "body", "gc.outcome": "pass"}}
        self.meta(root, scopes=[scope])
        return scope

    def document(self, wt, root="run-1"):
        doc = wt / self.meta(root)["survey_doc"]
        doc.write_text("# Survey\n\nActual source behavior.\n")
        self.run_survey("stamp", root=root)
        self.commit(wt)
        return doc

    def test_prepare_repeat_preserves_commit_dirty_work_and_modes(self):
        wt = self.prepare()
        self.assertEqual(self.meta()["survey_doc"], "docs/current-state.md")
        self.assertNotIn("survey_output_dir", self.meta())
        self.assertFalse((wt / "docs/current-state").exists())
        self.document(wt)
        head = self.git("rev-parse", "HEAD", cwd=wt)
        (wt / "source.txt").write_text("unfinished work\n")
        self.run_survey("prepare")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt), head)
        self.assertEqual((wt / "source.txt").read_text(), "unfinished work\n")
        self.assertEqual(self.meta()["survey_publish"], "none")
        self.run_survey("prepare", "--publish", "direct", ok=False)
        self.run_survey("prepare", "--output-file", "elsewhere.md", ok=False)
        self.run_survey("prepare", "--pr-tool", "glab", ok=False)
        other = self.prepare(root="run-2")
        self.assertNotEqual(wt, other)
        self.assertNotEqual(self.meta()["survey_branch"], self.meta("run-2")["survey_branch"])

    def test_custom_output_files_through_publish_and_cleanup(self):
        for root, output in (("nested", "reports/nested/state.md"),
                             ("top-level", "current-state.md")):
            with self.subTest(output=output):
                self.run_survey("prepare", "--output-file", output, "--publish", "direct", root=root)
                saved = self.meta(root).copy()
                self.assertEqual(saved["survey_doc"], output)
                self.assertNotIn("survey_output_dir", saved)
                wt = Path(saved["work_dir"])
                self.run_survey("prepare", root=root)
                self.run_survey("prepare", "--output-file", output, root=root)
                self.assertEqual(self.meta(root), saved)
                doc = self.document(wt, root)
                self.assertEqual(doc, wt / output)
                self.run_survey("publish", root=root)
                self.assertIn("Actual source behavior.", self.git("show", f"main:{output}", cwd=self.origin))
                self.scope_pass(root)
                self.run_survey("cleanup", root=root)
                self.assertFalse(wt.exists())

    def test_persisted_legacy_handoffs_keep_paths_and_dirty_work(self):
        for root, directory in (("legacy-default", "docs/current-state"),
                                ("legacy-custom", "reports/old-survey")):
            with self.subTest(directory=directory):
                # This is also the command retained in an old cooked prepare step.
                self.run_survey("prepare", "--output-dir", directory, "--publish", "direct", root=root)
                self.meta(root, survey_output_dir=directory)
                saved = self.meta(root).copy()
                self.assertEqual(saved["survey_doc"], f"{directory}/README.md")
                wt = Path(saved["work_dir"])
                doc = wt / saved["survey_doc"]
                doc.write_text("unfinished survey\n")
                self.run_survey("prepare", root=root)
                self.run_survey("prepare", "--output-dir", directory + "/", root=root)
                self.run_survey("prepare", "--output-file", saved["survey_doc"], root=root)
                self.run_survey("prepare", "--output-file", "docs/current-state.md", root=root, ok=False)
                self.assertEqual(self.meta(root), saved)
                self.assertEqual(doc.read_text(), "unfinished survey\n")
                self.assertFalse((wt / "docs/current-state.md").exists())
                self.meta(root, survey_doc="elsewhere.md")
                self.run_survey("stamp", root=root, ok=False)
                self.meta(root, **saved)
                self.document(wt, root)
                self.run_survey("publish", root=root)
                self.scope_pass(root)
                self.run_survey("cleanup", root=root)
                self.assertFalse(wt.exists())
                self.assertEqual(self.meta(root)["survey_doc"], saved["survey_doc"])
                self.assertEqual(self.meta(root)["survey_output_dir"], directory)

    def test_stamp_and_none_publish(self):
        wt = self.prepare()
        doc = self.document(wt)
        self.assertIn("status: draft\nsource: agent\n", doc.read_text())
        body = doc.read_text().split("---\n", 2)[2]
        result = self.run_survey("stamp")
        self.assertEqual(result.stdout, doc.read_text().split("---\n", 2)[1])
        self.assertEqual(doc.read_text().split("---\n", 2)[2], body)
        # Stamp timestamps can change; commit only if needed.
        if self.git("status", "--porcelain", cwd=wt):
            self.commit(wt)
        self.run_survey("publish")
        self.assertEqual(self.meta()["survey_publish_status"], "none")
        self.meta(**{"gc.outcome": "pass"})
        self.scope_pass()
        self.run_survey("cleanup")
        self.assertTrue(wt.exists())
        self.assertIn("publish=none", self.meta()["survey_cleanup"])

    def test_direct_publish_retry_and_cleanup_retry(self):
        wt = self.prepare("direct")
        self.document(wt)
        self.run_survey("publish")
        self.run_survey("publish")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt),
                         self.git("rev-parse", "main", cwd=self.origin))
        self.run_survey("cleanup")
        self.assertTrue(wt.exists())
        self.scope_pass()
        self.run_survey("cleanup")
        self.run_survey("cleanup")
        self.assertFalse(wt.exists())
        self.assertEqual(self.meta()["survey_cleanup"], "removed")
        self.run_survey("prepare", ok=False)

    def test_pr_publish_retry_for_both_forges(self):
        for tool in ("gh", "glab"):
            with self.subTest(tool=tool):
                root = f"run-{tool}"
                wt = self.prepare("pr", tool, root)
                doc = self.document(wt, root)
                self.assertIn("status: draft\nsource: agent\n", doc.read_text())
                self.run_survey("publish", root=root)
                self.run_survey("publish", root=root)
                self.assertEqual(self.meta(root)["survey_pr_url"], "https://forge.invalid/survey/1")
                calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
                creates = [args for name, args in calls if name == tool and args[1] == "create"]
                self.assertEqual(len(creates), 1)
                (self.root / "pr-url").unlink()

    def test_metadata_write_and_readback_fail_before_worktree_creation(self):
        for failure in ("error", "survey_base", "survey_pr_tool"):
            with self.subTest(failure=failure):
                (self.root / "state.json").write_text("{}")
                self.env["FAIL_METADATA"] = failure
                self.run_survey("prepare", ok=False)
                self.assertFalse((self.root / ".gc/worktrees/repo/current-state-run-1").exists())
        del self.env["FAIL_METADATA"]

    def test_worktree_creation_failure_keeps_recoverable_handoff(self):
        real_git = shutil.which("git")
        wrapper = self.bin / "git"
        wrapper.write_text(f'#!/bin/bash\nfor arg; do [[ "$arg" == add ]] && exit 1; done\nexec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
        self.run_survey("prepare", ok=False)
        self.assertIn("survey_base", self.meta())
        wrapper.unlink()
        self.run_survey("prepare")
        wt = Path(self.meta()["work_dir"])
        self.assertTrue(wt.exists())

    def test_unsafe_output_paths_and_symlinks(self):
        for output in ("/tmp/escape", "../escape", ".git", "docs/../escape", "", "docs//escape",
                       "docs/state.md/", "docs/state\n.md", "docs/state\r.md"):
            with self.subTest(output=output):
                self.run_survey("prepare", "--output-file", output, ok=False)
        self.run_survey("prepare", "--output-file", ok=False)
        self.run_survey("prepare", "--output-file", "state.md", "--output-dir", "docs", ok=False)
        self.run_survey("prepare", "--output-dir", "docs", "--output-file", "state.md", ok=False)
        self.run_survey("prepare", "--output-dir", "", ok=False)
        (self.repo / "directory.md").mkdir()
        self.run_survey("prepare", "--output-file", "directory.md", ok=False)
        (self.repo / "escape").symlink_to(self.root, target_is_directory=True)
        self.run_survey("prepare", "--output-file", "escape/state.md", ok=False)
        wt = self.prepare()
        doc = wt / self.meta()["survey_doc"]
        outside = self.root / "outside"
        outside.write_text("untouched")
        doc.symlink_to(outside)
        for command in ("stamp", "publish", "cleanup"):
            self.run_survey(command, ok=False)
        self.assertEqual(outside.read_text(), "untouched")

    def test_tampered_handoff_and_wrong_branch_are_refused_even_with_force(self):
        wt = self.prepare()
        original = self.meta().copy()
        for key, value in (("work_dir", str(self.repo)), ("survey_rig_root", str(self.root)),
                           ("survey_branch", "main"), ("survey_doc", "../outside"),
                           ("survey_base", "bad"), ("survey_publish", "bad")):
            with self.subTest(key=key):
                self.meta(**{key: value})
                self.run_survey("cleanup", "--force", ok=False)
                self.meta(**original)
        self.git("checkout", "-qb", "other", cwd=wt)
        self.run_survey("cleanup", "--force", ok=False)
        self.assertTrue(wt.exists())

    def test_publish_rejects_invalid_schema_dirty_and_source_commits(self):
        wt = self.prepare("direct")
        doc = self.document(wt)
        good = doc.read_text()
        for bad in ("no frontmatter\n", good.replace("status: draft", "status: accepted"),
                    good.replace("source: agent", "source: human")):
            doc.write_text(bad)
            self.commit(wt)
            self.run_survey("publish", ok=False)
        doc.write_text(good)
        self.commit(wt)
        (wt / "source.txt").write_text("changed\n")
        self.run_survey("publish", ok=False)
        self.commit(wt)
        self.run_survey("publish", ok=False)
        # A reverted source edit is still a source-changing commit in the push.
        (wt / "source.txt").write_text("source\n")
        self.commit(wt)
        self.run_survey("publish", ok=False)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), self.meta()["survey_base"])

    def test_unterminated_stamp_preserves_document_and_removes_temp(self):
        wt = self.prepare()
        doc = wt / self.meta()["survey_doc"]
        doc.write_text("---\nunfinished\n")
        self.run_survey("stamp", ok=False)
        self.assertEqual(doc.read_text(), "---\nunfinished\n")
        self.assertEqual(list(doc.parent.glob(".survey.*")), [])

    def test_rejected_push_preserves_commits_and_does_not_rebase(self):
        wt = self.prepare("direct")
        self.document(wt)
        head = self.git("rev-parse", "HEAD", cwd=wt)
        (self.repo / "source.txt").write_text("upstream\n")
        self.commit(self.repo)
        self.git("push", "origin", "main")
        self.run_survey("publish", ok=False)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt), head)
        self.assertNotIn("survey_publish_status", self.meta())

    def test_forge_failure_retries_after_push_without_duplicate_creation(self):
        wt = self.prepare("pr")
        self.document(wt)
        self.env["FAIL_FORGE"] = "list"
        self.run_survey("publish", ok=False)
        self.assertFalse((self.root / "pr-url").exists())
        del self.env["FAIL_FORGE"]
        self.env["FAIL_METADATA"] = "survey_pr_url"
        self.run_survey("publish", ok=False)
        del self.env["FAIL_METADATA"]
        self.run_survey("publish")
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(sum(name == "gh" and args[1] == "create" for name, args in calls), 1)

    def test_cleanup_preserves_dirty_ignored_and_unpublished_work(self):
        wt = self.prepare("direct")
        self.document(wt)
        self.scope_pass()
        self.run_survey("cleanup")
        self.assertTrue(wt.exists())
        self.run_survey("publish")
        extra = wt / "scratch"
        extra.write_text("precious")
        self.run_survey("cleanup")
        self.assertTrue(extra.exists())
        self.git("config", "core.excludesFile", str(self.root / "ignore"))
        (self.root / "ignore").write_text("scratch\n")
        self.run_survey("cleanup")
        self.assertTrue(extra.exists())
        self.run_survey("cleanup", "--force")
        self.assertFalse(wt.exists())

    def test_cleanup_recovers_after_remove_and_metadata_failure(self):
        wt = self.prepare("direct")
        self.document(wt)
        self.run_survey("publish")
        self.scope_pass()
        self.env["FAIL_METADATA"] = "survey_cleanup"
        self.run_survey("cleanup", ok=False)
        self.assertFalse(wt.exists())
        del self.env["FAIL_METADATA"]
        self.run_survey("prepare", ok=False)
        self.run_survey("cleanup")
        self.assertEqual(self.meta()["survey_cleanup"], "removed")

    def test_fetch_failure_and_missing_worktree_without_receipt(self):
        self.git("remote", "set-url", "origin", str(self.root / "missing.git"))
        self.run_survey("prepare", ok=False)
        self.assertEqual(self.meta(), {})
        self.git("remote", "set-url", "origin", str(self.origin))
        wt = self.prepare()
        self.git("worktree", "remove", str(wt))
        self.run_survey("cleanup", ok=False)

    def test_cleanup_requires_unique_closed_passing_scope_not_root_outcome(self):
        wt = self.prepare("direct")
        self.document(wt)
        self.run_survey("publish")
        self.meta(**{"gc.outcome": "pass"})
        good = self.scope_pass()
        for scopes in ([], [good, good], [dict(good, status="open")],
                       [dict(good, metadata=dict(good["metadata"], **{"gc.outcome": "fail"}))],
                       [dict(good, metadata=dict(good["metadata"], **{"gc.root_bead_id": "other"}))]):
            with self.subTest(scopes=scopes):
                self.meta(scopes=scopes)
                self.run_survey("cleanup")
                self.assertTrue(wt.exists())
        self.scope_pass()
        self.env["FAIL_SCOPE"] = "1"
        self.run_survey("cleanup")
        self.assertTrue(wt.exists())
        del self.env["FAIL_SCOPE"]
        self.meta(**{"gc.outcome": "fail"})
        self.run_survey("cleanup")
        self.assertFalse(wt.exists())

    def test_pr_push_never_overwrites_remote_commits(self):
        wt = self.prepare("pr")
        self.document(wt)
        self.run_survey("publish")
        branch = self.meta()["survey_branch"]
        self.git("fetch", "origin", branch)
        self.git("checkout", "-qb", "remote-edit", "FETCH_HEAD")
        (self.repo / "source.txt").write_text("remote-only work\n")
        self.commit(self.repo)
        remote_head = self.git("rev-parse", "HEAD")
        self.git("push", "origin", f"HEAD:refs/heads/{branch}")
        self.run_survey("publish", ok=False)
        self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), remote_head)
        self.scope_pass()
        self.run_survey("cleanup")
        self.assertTrue(wt.exists())

    def test_git_inspection_and_removal_failures_never_report_success(self):
        wt = self.prepare("direct")
        self.document(wt)
        real_git = shutil.which("git")
        wrapper = self.bin / "git"
        wrapper.write_text(f'#!/bin/bash\nfor arg; do [[ "$arg" == "$FAIL_GIT" ]] && exit 1; done\nexec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
        for operation in ("status", "log", "diff", "rev-list", "show"):
            with self.subTest(operation=operation):
                self.env["FAIL_GIT"] = operation
                self.run_survey("publish", ok=False)
                self.assertNotIn("survey_publish_status", self.meta())
        self.env["FAIL_GIT"] = "no-failure"
        self.run_survey("publish")
        self.scope_pass()
        self.env["FAIL_GIT"] = "remove"
        self.run_survey("cleanup", ok=False)
        self.assertTrue(wt.exists())
        self.env["FAIL_GIT"] = "branch"
        self.run_survey("cleanup", ok=False)
        self.assertFalse(wt.exists())
        self.env["FAIL_GIT"] = "no-failure"
        self.run_survey("cleanup")
        self.assertEqual(self.meta()["survey_cleanup"], "removed")

    def test_publish_validates_committed_content_not_skip_worktree_copy(self):
        wt = self.prepare("direct")
        doc = self.document(wt)
        valid = doc.read_text()
        doc.write_text("invalid committed schema\n")
        self.commit(wt)
        self.git("update-index", "--skip-worktree", self.meta()["survey_doc"], cwd=wt)
        doc.write_text(valid)
        self.assertEqual(self.git("status", "--porcelain", cwd=wt), "")
        self.run_survey("publish", ok=False)

    def test_identity_fallback_skips_hq_canonicalizes_and_lists_once(self):
        alias = self.root / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        (self.root / "rigs.json").write_text(json.dumps({"city_path": str(self.root), "rigs": [
            {"name": "hq", "path": str(self.root), "hq": True},
            {"name": "repo", "path": str(alias), "hq": False}]}))
        for key in ("GC_RIG", "GC_RIG_ROOT", "GC_CITY"):
            del self.env[key]
        self.run_survey("prepare", "--publish", "none", cwd=alias)
        self.assertEqual(self.meta()["survey_rig_root"], str(self.repo))
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(calls[0], ["gc", ["rig", "list", "--json"]])
        self.assertEqual(sum(args == ["rig", "list", "--json"] for _, args in calls), 1)
        for name, args in calls[1:]:
            self.assertEqual([name, *args[:3]], ["gc", "bd", "--rig", "repo"])
        self.run_survey("show", cwd=alias)

    def test_identity_failures_precede_all_bead_operations_including_show(self):
        for key in ("GC_RIG", "GC_RIG_ROOT", "GC_CITY"):
            del self.env[key]
        self.env["FAIL_RIG_LIST"] = "1"
        for command in ("prepare", "show", "stamp", "publish", "cleanup"):
            result = self.run_survey(command, ok=False)
            self.assertIn("rig registry unavailable", result.stderr)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(all(args == ["rig", "list", "--json"] for _, args in calls))
        del self.env["FAIL_RIG_LIST"]
        for payload in ("not json", '{}', '{"rigs": [], "city_path": "/missing"}'):
            (self.root / "rigs.json").write_text(payload)
            self.run_survey("show", ok=False)

    def test_identity_rejects_ambiguous_boundary_and_partial_env_conflicts(self):
        for key in ("GC_RIG", "GC_RIG_ROOT", "GC_CITY"):
            del self.env[key]
        sibling = self.root / "repo-other"
        sibling.mkdir()
        self.run_survey("show", cwd=sibling, ok=False)
        rigs = {"city_path": str(self.root), "rigs": [
            {"name": "repo", "path": str(self.repo)},
            {"name": "duplicate", "path": str(self.repo)}]}
        (self.root / "rigs.json").write_text(json.dumps(rigs))
        result = self.run_survey("show", ok=False)
        self.assertIn("found 2", result.stderr)
        rigs["rigs"].pop()
        (self.root / "rigs.json").write_text(json.dumps(rigs))
        self.env["GC_RIG"] = "other"
        self.run_survey("show", ok=False)
        del self.env["GC_RIG"]
        self.env["GC_CITY"] = str(sibling)
        self.run_survey("show", ok=False)

    def test_root_identity_and_store_must_match_before_any_mutation(self):
        for metadata in ({"gc.root_store_ref": "city:repo"}, {"gc.root_store_ref": "rig:other"},
                         {"gc.root_store_ref": None}, {"gc.kind": "task"},
                         {"gc.formula_name": "other"}, {"gc.root_bead_id": "other-root"},
                         {"gc.step_ref": "current-state-survey.survey"}):
            with self.subTest(metadata=metadata):
                (self.root / "state.json").write_text(json.dumps({"run-1": metadata}))
                for command in ("prepare", "show", "stamp", "publish", "cleanup"):
                    self.run_survey(command, ok=False)
        self.env["FAIL_ROOT_ID"] = "different-id"
        self.run_survey("show", ok=False)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(all(args[:4] == ["bd", "--rig", "repo", "show"] for _, args in calls))
        self.assertFalse((self.root / ".gc").exists())

    def test_pr_body_uses_recorded_base_when_remote_tracking_ref_moves(self):
        wt = self.prepare("pr")
        self.document(wt)
        base = self.meta()["survey_base"]
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD", cwd=wt))
        self.run_survey("publish")
        body = (self.root / "pr-body").read_text()
        self.assertIn(f"recorded base `{self.git('rev-parse', '--short', base)}`", body)
        self.assertIn("1 file changed", body)

    def test_auto_pr_tool_error_recommends_new_root(self):
        wt = self.prepare("pr", "auto")
        self.document(wt)
        result = self.run_survey("publish", ok=False)
        self.assertIn("prepare a new root with --pr-tool gh|glab", result.stderr)

    def test_pr_body_file_uses_caller_directory_and_failure_precedes_push(self):
        for tool in ("gh", "glab"):
            with self.subTest(tool=tool):
                root = f"run-{tool}"
                wt = self.prepare("pr", tool, root)
                self.document(wt, root)
                self.run_survey("publish", "--body-file", "missing.txt", root=root, ok=False)
                self.assertEqual(self.git("ls-remote", "--heads", "origin", self.meta(root)["survey_branch"]), "")
                (self.repo / "body.txt").write_text("Caller-supplied body.\n")
                self.run_survey("publish", "--body-file", "body.txt", root=root)
                self.assertEqual((self.root / "pr-body").read_text().strip(), "Caller-supplied body.")
                (self.root / "pr-url").unlink()

    def test_root_validation_accepts_real_isolated_formula_metadata(self):
        from test.test_survey_integration import SurveyIntegrationTest

        SurveyIntegrationTest.setUpClass()
        fixture = SurveyIntegrationTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        rig_state = fixture.root / "widgets/.gc"
        rig_state.mkdir()
        (rig_state / "beads.json").write_text('{"seq":0,"beads":[]}\n')
        fixture.run_command(fixture.gc, "formula", "cook", "current-state-survey", "--rig", "widgets",
                            "--var", "publish=none", "--json")
        beads = json.loads((rig_state / "beads.json").read_text())["beads"]
        root = next(bead for bead in beads if bead.get("metadata", {}).get("gc.kind") == "workflow")
        metadata = root["metadata"]
        self.assertEqual(metadata["gc.root_store_ref"], "rig:widgets")
        self.assertEqual(metadata["gc.formula_name"], "current-state-survey")
        # Only translate the fixture rig. Preserve the actual root ID and every
        # other compiler field when exercising the script through mocked gc.
        self.meta(root["id"], **dict(metadata, **{"gc.root_store_ref": "rig:repo"}))
        self.run_survey("show", root=root["id"])


if __name__ == "__main__":
    unittest.main()
