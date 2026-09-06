"""Installed-gc survey contracts in unregistered, file-provider fixture cities."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

from test import test_commands


class SurveyIntegrationTest(test_commands.GasCityCompatibilityTest):
    # The shared fixture owns these tests in test_commands; do not run them twice.
    test_rendered_rig_brief_dispatches_through_real_gc = None
    test_archivist_renders_writer_instructions_and_arbitration = None
    test_coordinator_briefs_and_fragment_gates = None
    test_formula_commands_dispatch_and_preserve_requests_and_failures = None

    def setUp(self):
        super().setUp()
        source = Path(__file__).resolve().parents[1]
        for name in ("formulas", "orders", "assets/workflows", "commands/survey"):
            shutil.copytree(source / name, self.pack / name, dirs_exist_ok=True)
        config = self.root / "city.toml"
        config.write_text(config.read_text() +
                          '[rigs.imports.hindsight]\nsource = ' + json.dumps(str(self.pack)) + '\n')
        dispatcher = self.root / "agents/control-dispatcher"
        dispatcher.mkdir()
        (dispatcher / "agent.toml").write_text('dir = "widgets"\n')

    def test_surveyor_prompt_and_dispatch(self):
        prompt = self.render("widgets/hindsight.surveyor")
        self.assertIn("rig `widgets`", prompt)
        self.assertIn(str(self.root / "widgets"), prompt)
        self.assertIn("gc hindsight survey", prompt)
        self.mock(self.pack / "assets/scripts/survey.sh")
        command = re.search(r"`(gc hindsight survey <subcommand> <root-bead-id>)`", prompt)[1]
        self.run_command("bash", "-eu", "-c", command.replace(
            "<subcommand>", "show").replace("<root-bead-id>", "fixture-root"))
        self.assertEqual(self.calls()[-1]["args"], ["show", "fixture-root"])

    def test_rig_list_shape(self):
        result = self.run_command(self.gc, "rig", "list", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(Path(payload["city_path"]).resolve(), self.root)
        rigs = [r for r in payload["rigs"] if not r["hq"]]
        self.assertEqual(rigs[0]["name"], "widgets")
        self.assertEqual(Path(rigs[0]["path"]).resolve(), self.root / "widgets")

    def test_formula_compiles_description_files(self):
        result = self.run_command(self.gc, "formula", "show", "current-state-survey",
                                  "--rig", "widgets", "--json")
        self.assertIn("Prepare the survey worktree", result.stdout)
        self.assertIn("survey prepare", result.stdout)
        recipe = json.loads(result.stdout)
        steps = {s["id"].removeprefix("current-state-survey."): s for s in recipe["steps"]}
        survey = steps["survey"]["description"]
        self.assertLess(survey.index("survey stamp"), survey.index("git commit -S"))
        self.assertEqual(len(re.findall(r"(?m)^ +git commit ", survey)), 1)
        self.assertIn('--only', survey)
        self.assertIn('git add -- "{{output_dir}}/README.md"', survey)
        self.assertIn("scope-body bead", steps["cleanup"]["description"])
        deps = {(d["step_id"].split(".")[-1], d["depends_on_id"].split(".")[-1])
                for d in recipe["deps"] if d["type"] == "blocks"}
        self.assertIn(("cleanup", "worktree"), deps)
        self.assertIn(("workflow-finalize", "worktree"), deps)
        self.assertNotIn(("cleanup", "workflow-finalize"), deps)
        self.assertNotIn(("workflow-finalize", "cleanup"), deps)

    def test_missing_description_file_fails_compilation(self):
        (self.pack / "assets/workflows/current-state-survey/survey.md").unlink()
        result = self.run_command(self.gc, "formula", "show", "current-state-survey",
                                  "--rig", "widgets", "--json", code=1)
        self.assertIn("survey.md", result.stdout + result.stderr)

    def test_order_var_reaches_safe_fixture_dispatch(self):
        # Without a scope-local file, gc's legacy file provider aliases the rig
        # to the city store. Seed the rig explicitly to exercise real routing.
        rig_state = self.root / "widgets/.gc"
        rig_state.mkdir()
        (rig_state / "beads.json").write_text('{"seq":0,"beads":[]}\n')
        result = self.run_command(self.gc, "order", "run", "current-state-survey",
                                  "--rig", "widgets", "--var", "publish=none", "--json")
        result = json.loads(result.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["routed_to"], "widgets/hindsight.surveyor")
        store = json.loads((rig_state / "beads.json").read_text())
        steps = {b.get("metadata", {}).get("gc.step_ref"): b for b in store["beads"]}
        prepare = steps["current-state-survey.prepare-worktree"]
        # Installed gc accepts --var but loses overrides during order
        # instantiation. Pin the observed behavior so docs cannot call this a
        # local-only run. Revisit this assertion when upstream fixes dispatch.
        self.assertIn('--publish "pr"', prepare["description"])
        self.assertNotIn("{{", prepare["description"])
        for name in ("prepare-worktree", "survey", "publish", "cleanup"):
            bead = steps["current-state-survey." + name]
            self.assertEqual(bead["metadata"]["gc.root_bead_id"], result["wisp_id"])
            self.assertEqual(bead["metadata"]["gc.routed_to"], "widgets/hindsight.surveyor")
        self.mock(self.pack / "assets/scripts/survey.sh")
        command = re.search(r"(?m)^ +(gc hindsight survey prepare .*)$", prepare["description"])[1]
        self.run_command("bash", "-eu", "-c", command.replace("<root>", result["wisp_id"]))
        self.assertEqual(self.calls()[-1]["args"], [
            "prepare", result["wisp_id"], "--output-dir", "docs/current-state",
            "--publish", "pr", "--pr-tool", "auto"])

    def test_formula_cook_preserves_local_only_override(self):
        rig_state = self.root / "widgets/.gc"
        rig_state.mkdir()
        (rig_state / "beads.json").write_text('{"seq":0,"beads":[]}\n')
        self.run_command(self.gc, "formula", "cook", "current-state-survey", "--rig", "widgets",
                         "--var", "publish=none", "--json")
        store = json.loads((rig_state / "beads.json").read_text())
        prepare = next(b for b in store["beads"] if b.get("metadata", {}).get("gc.step_ref")
                       == "current-state-survey.prepare-worktree")
        self.assertIn('--publish "none"', prepare["description"])
        self.assertEqual(prepare["metadata"]["gc.routed_to"], "widgets/hindsight.surveyor")
        self.assertEqual(prepare["metadata"]["gc.root_store_ref"], "rig:widgets")

        # Execute the real scope check while the finalizer is still open.
        # These mutations affect only the isolated file-provider fixture.
        for outcome in ("pass", "fail"):
            with self.subTest(scope_outcome=outcome):
                snapshot = json.loads(json.dumps(store))
                for bead in snapshot["beads"]:
                    ref = bead.get("metadata", {}).get("gc.step_ref", "")
                    if ref in {"current-state-survey." + name for name in (
                            "prepare-worktree", "prepare-worktree-scope-check",
                            "survey", "survey-scope-check", "publish")}:
                        bead["status"] = "closed"
                        bead["metadata"]["gc.outcome"] = outcome if ref.endswith(".publish") else "pass"
                check = next(b for b in snapshot["beads"] if b.get("metadata", {}).get("gc.step_ref")
                             == "current-state-survey.publish-scope-check")
                (rig_state / "beads.json").write_text(json.dumps(snapshot))
                self.run_command(self.gc, "convoy", "control", check["id"])
                after = json.loads((rig_state / "beads.json").read_text())
                by_ref = {b.get("metadata", {}).get("gc.step_ref"): b for b in after["beads"]}
                body = by_ref["current-state-survey.worktree"]
                self.assertEqual(body["status"], "closed")
                self.assertEqual(body["metadata"]["gc.outcome"], outcome)
                root = next(b for b in after["beads"] if b["id"] == prepare["metadata"]["gc.root_bead_id"])
                self.assertNotIn("gc.outcome", root["metadata"])
                self.assertEqual(by_ref["current-state-survey.cleanup"]["status"], "open")

    def test_bd_scope_and_session_environment_outside_rig(self):
        # Capture real gc's bd forwarding, never invoke an installed bd binary.
        config = self.root / "city.toml"
        config.write_text(config.read_text().replace('provider = "file"', 'provider = "bd"'))
        (self.bin / "bd").write_text(f"#!{sys.executable}\n" +
            'import json, os\nprint(json.dumps({k: os.environ.get(k) for k in '
            '("GC_STORE_ROOT", "GC_STORE_SCOPE", "GC_RIG", "GC_RIG_ROOT")}))\n')
        worktree = self.root / ".gc/worktrees/widgets/survey"
        worktree.mkdir(parents=True)
        self.env.update(GC_RIG="widgets", GC_RIG_ROOT=str(self.root / "widgets"))
        for flags, scope, root in (([], "rig", self.root / "widgets"),
                                   (["--rig", "widgets"], "rig", self.root / "widgets"),
                                   (["--city", str(self.root)], "city", self.root)):
            with self.subTest(flags=flags):
                result = subprocess.run([self.gc, "bd", *flags, "show", "fixture-root", "--json"],
                                        cwd=worktree, env=self.env, text=True,
                                        capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                env = json.loads(result.stdout)
                self.assertEqual(env["GC_STORE_SCOPE"], scope)
                self.assertEqual(Path(env["GC_STORE_ROOT"]).resolve(), root)
                self.assertEqual(env["GC_RIG"], "widgets" if scope == "rig" else "")
                if scope == "rig":
                    self.assertEqual(Path(env["GC_RIG_ROOT"]).resolve(), root)
        # bd environment construction makes an empty data directory even
        # with a stub binary. No database may have been initialized there.
        for scope_root in (self.root, self.root / "widgets"):
            data = scope_root / ".beads/dolt"
            if data.exists():
                self.assertEqual(list(data.iterdir()), [])
                data.rmdir()


if __name__ == "__main__":
    unittest.main()
