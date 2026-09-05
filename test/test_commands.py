"""Offline rendering and pack dispatch tests. Never invoke a real gc or bank."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest


PACK = Path(__file__).resolve().parents[1]
HELPERS = {
    "read": "hindsight-read.sh",
    "maintain": "bank-maintain.sh",
    "retain": "memory-retain.sh",
    "ship": "ship-docs.sh",
}
MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'gc':
    if len(args) < 2 or args[0] != 'hindsight' or args[1] not in ('read', 'maintain', 'retain', 'ship'):
        sys.exit('unexpected gc dispatch: ' + repr(args))
    script = pathlib.Path(os.environ['MOCK_PACK']) / 'commands' / args[1] / 'run.sh'
    os.execv(str(script), [str(script), *args[2:]])
entry = dict(tool=name, args=args, api=os.environ.get('HINDSIGHT_API_URL'),
             key=os.environ.get('HINDSIGHT_API_KEY'))
if name.endswith('.sh'):
    entry['stdin'] = sys.stdin.read()
with open(os.environ['MOCK_LOG'], 'a') as log:
    log.write(json.dumps(entry) + '\n')
if name == 'curl':
    sys.exit(97)  # Fail closed: a gate test must never reach a real endpoint.
print(json.dumps({'content': 'fixture brief', 'results': [], 'items': []}))
print('fixture stderr', file=sys.stderr)
sys.exit(int(os.environ.get('MOCK_EXIT', '0')))
'''


class CommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="hindsight-render-")
        cls.addClassCleanup(cls.build.cleanup)
        cls.renderer = Path(cls.build.name) / "render-prompt"
        # Only the standard library is needed. Disable module/network lookup.
        env = dict(os.environ, GOWORK="off", GOPROXY="off", GOTOOLCHAIN="local")
        subprocess.run(
            ["go", "build", "-o", str(cls.renderer), str(PACK / "test/assets/render_prompt.go")],
            cwd=cls.build.name, env=env, check=True, capture_output=True, text=True,
            timeout=120,
        )

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hindsight-commands-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.pack = self.root / "installed pack"
        for name in HELPERS:
            shutil.copytree(PACK / "commands" / name, self.pack / "commands" / name)
        shutil.copytree(PACK / "assets/scripts", self.pack / "assets/scripts")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("gc", "hindsight", "curl"):
            self.mock(self.bin / name)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("HINDSIGHT_", "GC_", "MOCK_"))}
        self.env.update(
            PATH=f"{self.bin}:{os.environ['PATH']}", HOME=str(self.root),
            TMPDIR=str(self.root), TMP=str(self.root), MOCK_PACK=str(self.pack),
            MOCK_LOG=str(self.root / "calls.jsonl"), HINDSIGHT_BANK="fixture bank",
            HINDSIGHT_API="https://fixture.invalid/", HINDSIGHT_API_KEY="fixture-key",
            HINDSIGHT_CONFIG=str(self.root / "no-config"),
        )

    def mock(self, path):
        path.write_text(MOCK)
        path.chmod(0o755)

    def run_command(self, *args, code=0, input=""):
        result = subprocess.run(
            args, cwd=self.root, env=self.env, input=input,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def calls(self):
        path = Path(self.env["MOCK_LOG"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def writer(self):
        self.env.update(HINDSIGHT_WRITER="archivist", GC_SESSION_ID="fixture-session")

    def render(self, root='{{template "hindsight-brief" .}}', fragments=(), **data):
        context = dict(AgentName="widgets/delivery-1", TemplateName="delivery",
                       BindingName="delivery-pack", RigName="widgets", HINDSIGHT_MEMORY="1")
        context.update(data)
        payload = dict(Root=root, Data=context, Fragments=[
            (PACK / "template-fragments/hindsight.template.md").read_text(), *fragments,
        ])
        result = self.run_command(str(self.renderer), input=json.dumps(payload))
        self.assertNotIn("/assets/", result.stdout)
        self.assertNotIn("<no value>", result.stdout)
        self.assertNotIn("{{", result.stdout)
        return result.stdout

    def test_rendered_rig_reflect_dispatches_without_config_dir(self):
        rendered = self.render()
        # Execute the rendered shell block, not a separately hardcoded command.
        command = re.search(r"(?m)^ {4}(\S.*)$", rendered.replace("\\\n", "")).group(1)
        self.run_command("bash", "-eu", "-c", command)
        self.assertEqual(self.calls()[0]["args"], [
            "memory", "reflect", "fixture bank", "<the task, verbatim>",
            "--tags", "repo:widgets,scope:platform", "--tags-match", "any_strict", "--budget", "mid",
        ])

    def test_real_gc_discovers_and_dispatches_read_without_starting_city(self):
        gc = shutil.which("gc")
        if gc is None:
            self.skipTest("Gas City is required for pack-discovery verification")
        # Write a fixture config directly. gc init would register/start a city.
        (self.root / "city.toml").write_text(
            '[workspace]\nname = "hindsight-offline"\n[imports.hindsight]\nsource = '
            + json.dumps(str(PACK)) + '\n[beads]\nprovider = "file"\n')
        for key in list(self.env):
            if key.startswith(("BEADS_", "BD_", "DOLT_", "GT_")):
                self.env.pop(key)
        self.env["GC_DISABLE_USAGE_METRICS"] = "1"
        self.run_command(gc, "hindsight", "read", "-o", "json",
                         "bank", "stats", "fixture bank")
        self.assertEqual(self.calls()[-1]["tool"], "hindsight")
        self.assertEqual(self.calls()[-1]["args"], ["-o", "json", "bank", "stats", "fixture bank"])
        self.assertFalse((self.root / ".gc/supervisor.pid").exists())

    def test_rendered_coordinator_and_standing_briefs_dispatch(self):
        rendered = self.render(RigName="", HINDSIGHT_MENTAL_MODELS="landmines conventions-and-standards")
        command = re.search(r"(?m)^ {4}(\S.*)$", rendered.replace("\\\n", "")).group(1)
        # <rig> is a documented user placeholder, not shell syntax to execute.
        self.run_command("bash", "-eu", "-c", command.replace("<rig>", "widgets"))
        loop = re.search(r"(?ms)^ {4}TMP=.*?^ {4}done$", rendered).group(0)
        result = self.run_command("bash", "-eu", "-c", loop)
        self.assertEqual(result.stdout.splitlines(), ["fixture brief", "fixture brief"])
        self.assertIn("repo:widgets,scope:platform,scope:business", self.calls()[0]["args"])
        self.assertEqual([c["args"] for c in self.calls()[1:]], [
            ["-o", "json", "mental-model", "get", "fixture bank", model]
            for model in ("landmines", "conventions-and-standards")
        ])

    def test_fragment_gates_and_specialization_survive_rendering(self):
        self.assertEqual(self.render(HINDSIGHT_MEMORY=""), "")
        root = '{{template "hindsight-propose" .}}'
        self.assertEqual(self.render(root), "")
        self.assertIn("gc mail send memory.archivist", self.render(
            root, HINDSIGHT_PROPOSE="1", HINDSIGHT_ARCHIVIST="memory.archivist"))
        self.assertIn("gc mail send archivist", self.render(root, HINDSIGHT_PROPOSE="1"))
        fragments = (
            '{{define "hindsight-brief-delivery"}}pool override{{end}}',
            '{{define "hindsight-brief-widgets/delivery-1"}}agent override{{end}}',
        )
        self.assertEqual(self.render(fragments=fragments), "agent override")
        self.assertEqual(self.render(fragments=fragments[:1]), "pool override")

    def test_archivist_renders_command_instructions_and_writer_rules(self):
        root = (PACK / "agents/archivist/prompt.template.md").read_text()
        root += '\n{{template "hindsight-arbitrate" .}}'
        rendered = self.render(root, AgentName="hindsight.archivist", TemplateName="archivist")
        for name in ("ship", "retain", "maintain"):
            self.assertIn(f"gc hindsight {name}", rendered)
        for rule in ("HINDSIGHT_WRITER=archivist", "one at a time in the foreground",
                     "never pass that marker", "deny is the default"):
            self.assertIn(rule, rendered)

    def test_wrappers_preserve_argv_stdin_output_and_exit_status(self):
        args = ["--bank", "bank with spaces", "", "literal $(no-execution); *", "--unknown"]
        for name in ("read", "maintain", "retain"):
            with self.subTest(command=name):
                self.mock(self.pack / "assets/scripts" / HELPERS[name])
                self.env["MOCK_EXIT"] = "23"
                result = self.run_command("gc", "hindsight", name, *args, input="exact content\n", code=23)
                call = self.calls()[-1]
                self.assertEqual(call["tool"], HELPERS[name])
                self.assertEqual(call["args"], args)
                self.assertEqual(call["stdin"], "exact content\n")
                self.assertEqual(json.loads(result.stdout)["content"], "fixture brief")
                self.assertEqual(result.stderr, "fixture stderr\n")

    def test_read_uses_existing_allowlist_and_connection(self):
        for operation in ("memory recall", "memory reflect", "document get", "document list",
                          "mental-model get", "mental-model list", "operation get", "operation list",
                          "bank config", "bank stats", "tag list"):
            with self.subTest(operation=operation):
                args = ["--output", "json", *operation.split(), "fixture bank"]
                self.run_command("gc", "hindsight", "read", *args)
                self.assertEqual(self.calls()[-1]["args"], args)
                self.assertEqual(self.calls()[-1]["api"], "https://fixture.invalid")
                self.assertEqual(self.calls()[-1]["key"], "fixture-key")
        self.env["MOCK_EXIT"] = "19"
        self.run_command("gc", "hindsight", "read", "bank", "stats", "fixture bank", code=19)

    def test_read_rejects_writes_before_downstream_dispatch(self):
        for operation in ("memory retain", "memory clear", "bank consolidate", "bank delete",
                          "mental-model refresh", "document delete", "unknown command"):
            with self.subTest(operation=operation):
                self.run_command("gc", "hindsight", "read", "-o", "json", *operation.split(), code=2)
        self.assertEqual(self.calls(), [])

    def test_writer_commands_do_not_bypass_admission(self):
        for marker, session in (("", ""), ("archivist", ""), ("worker", "fixture-session")):
            self.env.update(HINDSIGHT_WRITER=marker, GC_SESSION_ID=session)
            for name in ("maintain", "retain"):
                with self.subTest(command=name, marker=marker, session=session):
                    result = self.run_command("gc", "hindsight", name, code=2)
                    self.assertIn("managed archivist", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_admitted_writers_and_read_only_audit_reach_existing_drain(self):
        self.run_command("gc", "hindsight", "maintain", "--skip-consolidate", code=5)
        self.writer()
        self.run_command("gc", "hindsight", "maintain", code=5)
        self.run_command("gc", "hindsight", "retain", "--id", "gotcha.fixture", code=5)
        self.assertEqual([c["tool"] for c in self.calls()], ["curl"] * 3)

    def test_ship_formula_dispatches_empty_and_encoded_requests(self):
        formula = tomllib.loads((PACK / "formulas/mol-hindsight-ship.toml").read_text())
        description = formula["steps"][0]["description"]
        command = re.search(r"(?m)^ {4}(\S.*)$", description).group(1)
        self.mock(self.pack / "assets/scripts/ship-docs.sh")
        self.writer()
        rendered = command.replace("{{request}}", "").replace("{{docs_roots}}", "'scheduled docs'")
        self.run_command("bash", "-eu", "-c", rendered)
        self.assertEqual(self.calls()[-1]["args"], ["--fetch", "--bank", "fixture bank", "scheduled docs"])
        args = ["--bank", "request bank", "--ref", "branch with spaces", "--reprocess",
                str(self.root / "docs with spaces/$(no-execution)")]
        request = base64.b64encode(json.dumps(args).encode()).decode()
        rendered = command.replace("{{request}}", request).replace("{{docs_roots}}", "ignored-default")
        self.run_command("bash", "-eu", "-c", rendered)
        self.assertEqual(self.calls()[-1]["args"], args)
        self.assertEqual(len(self.calls()), 2)  # No recursive queue or default-arg leakage.
        self.env.pop("HINDSIGHT_WRITER")
        self.run_command("bash", "-eu", "-c", rendered, code=2)
        self.assertEqual(len(self.calls()), 2)

    def test_maintenance_formula_dispatches_and_audit_reads(self):
        formula = tomllib.loads((PACK / "formulas/mol-hindsight-consolidate.toml").read_text())
        command = re.search(r"(?m)^ {4}(\S.*)$", formula["steps"][0]["description"]).group(1)
        self.mock(self.pack / "assets/scripts/bank-maintain.sh")
        for status in (0, 2, 3, 4, 5):
            self.env["MOCK_EXIT"] = str(status)
            self.run_command("bash", "-eu", "-c", command, code=status)
            self.assertEqual(self.calls()[-1]["tool"], "bank-maintain.sh")
        self.env.pop("MOCK_EXIT")
        audit = formula["steps"][1]["description"]
        command = re.findall(r"\(`([^`]+)`\)", audit)[-1].replace("<id>", "landmines")
        self.run_command("bash", "-eu", "-c", command)
        self.assertEqual(self.calls()[-1]["args"], [
            "-o", "json", "mental-model", "get", "fixture bank", "landmines",
        ])

    def test_owned_instructions_have_no_unresolved_pack_paths(self):
        for relative in ("template-fragments/hindsight.template.md", "agents/archivist/prompt.template.md",
                         "skills/hindsight-memory/SKILL.md", "skills/hindsight-shipping/SKILL.md",
                         "formulas/mol-hindsight-ship.toml", "formulas/mol-hindsight-consolidate.toml"):
            with self.subTest(path=relative):
                text = (PACK / relative).read_text()
                self.assertNotIn("{{.ConfigDir}}", text)
                self.assertNotIn("<pack>/", text)
                self.assertNotIn(".BindingName", text)


if __name__ == "__main__":
    unittest.main()
