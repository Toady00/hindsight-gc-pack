"""Command contracts and real-gc compatibility, without a registered city or services.

Run compatibility alone: python3 test/test_commands.py GasCityCompatibilityTest
GC_TEST_BIN overrides PATH discovery; a missing gc skips only compatibility tests.
"""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
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
MOCK = r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
entry = dict(tool=name, args=sys.argv[1:], api=os.environ.get('HINDSIGHT_API_URL'),
             key=os.environ.get('HINDSIGHT_API_KEY'))
if name.endswith('.sh'):
    entry['stdin'] = sys.stdin.read()
with open(os.environ['MOCK_LOG'], 'a') as log:
    log.write(json.dumps(entry) + '\n')
if name != 'hindsight' and not name.endswith('.sh'):
    sys.exit(97)  # Fail closed, including curl and accidental gc/bd/dolt calls.
print(json.dumps({'content': 'fixture brief', 'results': [], 'items': []}))
print('fixture stderr', file=sys.stderr)
sys.exit(int(os.environ.get('MOCK_EXIT', '0')))
'''


class CommandFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hindsight-commands-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.pack = self.root / "installed pack"
        for name in HELPERS:
            shutil.copytree(PACK / "commands" / name, self.pack / "commands" / name)
        shutil.copytree(PACK / "assets/scripts", self.pack / "assets/scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("gc", "hindsight", "curl", "bd", "dolt"):
            self.mock(self.bin / name)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("HINDSIGHT_", "GC_", "MOCK_", "BEADS_", "BD_", "DOLT_", "GT_"))}
        self.env.update(
            PATH=f"{self.bin}:{os.environ['PATH']}", HOME=str(self.root),
            TMPDIR=str(self.root), TMP=str(self.root),
            MOCK_LOG=str(self.root / "calls.jsonl"), HINDSIGHT_BANK="fixture bank",
            HINDSIGHT_API="https://fixture.invalid/", HINDSIGHT_API_KEY="fixture-key",
            HINDSIGHT_CONFIG=str(self.root / "no-config"),
        )

    def mock(self, path):
        path.write_text(f"#!{sys.executable}\n" + MOCK)
        path.chmod(0o755)

    def run_command(self, *args, code=0, input=""):
        result = subprocess.run(
            args, cwd=self.root, env=self.env, input=input,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def wrapper(self, name, *args, **kwargs):
        return self.run_command(str(self.pack / "commands" / name / "run.sh"), *args, **kwargs)

    def calls(self):
        path = Path(self.env["MOCK_LOG"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def writer(self):
        self.env.update(HINDSIGHT_WRITER="archivist", GC_SESSION_ID="fixture-session")


class CommandTest(CommandFixture):
    def test_wrappers_preserve_argv_stdin_output_and_exit_status(self):
        args = ["--bank", "bank with spaces", "", "literal $(no-execution); *", "--unknown"]
        for name in ("read", "maintain", "retain"):
            with self.subTest(command=name):
                self.mock(self.pack / "assets/scripts" / HELPERS[name])
                self.env["MOCK_EXIT"] = "23"
                result = self.wrapper(name, *args, input="exact content\n", code=23)
                call = self.calls()[-1]
                self.assertEqual(call["tool"], HELPERS[name])
                self.assertEqual(call["args"], args)
                self.assertEqual(call["stdin"], "exact content\n")
                self.assertEqual(json.loads(result.stdout)["content"], "fixture brief")
                self.assertEqual(result.stderr, "fixture stderr\n")

    def test_read_admits_reads_and_preserves_connection_and_exit_status(self):
        for prefix, operation in (([], "memory recall"), (["--output", "json"], "document list"),
                                  (["-o", "json"], "bank stats")):
            with self.subTest(operation=operation):
                args = [*prefix, *operation.split(), "fixture bank"]
                self.env["MOCK_EXIT"] = "19"
                self.wrapper("read", *args, code=19)
                self.assertEqual(self.calls()[-1], dict(
                    tool="hindsight", args=args, api="https://fixture.invalid", key="fixture-key"))

    def test_read_rejects_writes_and_unknown_operations_before_dispatch(self):
        for args in (("memory", "retain"), ("-o", "json", "mental-model", "refresh"),
                     ("unknown", "command"), ()):
            with self.subTest(args=args):
                result = self.wrapper("read", *args, code=2)
                self.assertIn("read operations only", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_writer_commands_require_both_archivist_marker_and_session(self):
        for marker, session in (("", ""), ("archivist", ""), ("worker", "fixture-session")):
            self.env.update(HINDSIGHT_WRITER=marker, GC_SESSION_ID=session)
            for name in ("maintain", "retain"):
                with self.subTest(command=name, marker=marker, session=session):
                    result = self.wrapper(name, code=2)
                    self.assertIn("managed archivist", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_admitted_writers_and_read_only_audit_reach_existing_drain(self):
        self.wrapper("maintain", "--skip-consolidate", code=5)
        self.writer()
        self.wrapper("maintain", code=5)
        self.wrapper("retain", "--id", "gotcha.fixture", code=5)
        self.assertEqual([c["tool"] for c in self.calls()], ["curl"] * 3)


class GasCityCompatibilityTest(CommandFixture):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("GC_TEST_BIN")
        binary = str(Path(configured).expanduser().resolve()) if configured else shutil.which("gc")
        if binary is None:
            raise unittest.SkipTest("Gas City not installed; set GC_TEST_BIN or put gc on PATH")
        if not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"GC_TEST_BIN is not executable: {binary}")
        cls.gc = binary

    def setUp(self):
        super().setUp()
        # Follow gascity-packs/tests/test_gc_role_prompt_integration.py: write
        # config and site paths directly. NEVER gc init/start or use --hook.
        for name in ("home", "gc-home", ".gc", "widgets"):
            (self.root / name).mkdir()
        for name in ("pack.toml", "agents", "template-fragments"):
            source = PACK / name
            if source.is_dir():
                shutil.copytree(source, self.pack / name)
            else:
                shutil.copy2(source, self.pack / name)
        (self.root / "pack.toml").write_text(
            '[pack]\nname = "command-integration"\nschema = 2\n'
            '[imports.hindsight]\nsource = ' + json.dumps(str(self.pack)) + '\n')
        (self.root / "city.toml").write_text(
            '[workspace]\nprovider = "codex"\n'
            '[providers.codex]\nbase = "builtin:codex"\n'
            '[beads]\nprovider = "file"\n[[rigs]]\nname = "widgets"\n')
        (self.root / ".gc/site.toml").write_text(
            'workspace_name = "command-integration"\n[[rig]]\nname = "widgets"\npath = '
            + json.dumps(str(self.root / "widgets")) + '\n')
        agent = self.root / "agents/reader"
        agent.mkdir(parents=True)
        (agent / "agent.toml").write_text(
            'dir = "widgets"\n[env]\nHINDSIGHT_MEMORY = "1"\nHINDSIGHT_PROPOSE = "1"\n'
            'HINDSIGHT_ARCHIVIST = "hindsight.archivist"\n')
        (agent / "prompt.template.md").write_text(
            '# Fixture reader\n{{template "hindsight-brief" .}}\n{{template "hindsight-propose" .}}\n')
        self.env.update(
            HOME=str(self.root / "home"), GC_HOME=str(self.root / "gc-home"),
            XDG_CONFIG_HOME=str(self.root / "home/.config"),
            GC_CITY=str(self.root), GC_CITY_PATH=str(self.root), GC_CITY_ROOT=str(self.root),
            GC_DISABLE_USAGE_METRICS="1",
        )
        (self.bin / "gc").unlink()
        (self.bin / "gc").symlink_to(self.gc)

    def tearDown(self):
        self.assertFalse((self.root / "gc-home/cities.toml").exists())
        self.assertFalse((self.root / "gc-home/supervisor.pid").exists())
        self.assertFalse((self.root / ".gc/supervisor.pid").exists())
        self.assertFalse((self.root / ".beads/dolt").exists())

    def render(self, agent):
        prompt = self.run_command(self.gc, "--city", str(self.root), "prime", "--strict", agent).stdout
        for unresolved in ("{{", "<no value>", "/assets/", "<pack>/"):
            self.assertNotIn(unresolved, prompt)
        return prompt

    def test_rendered_rig_brief_dispatches_through_real_gc(self):
        prompt = self.render("widgets/reader")
        self.assertIn("# Fixture reader", prompt)
        self.assertIn("gc mail send hindsight.archivist", prompt)
        command = re.search(r"(?m)^ {4}(gc hindsight read .*?)$", prompt.replace("\\\n", "")).group(1)
        self.run_command("bash", "-eu", "-c", command)
        self.assertEqual([c["args"] for c in self.calls()], [[
            "memory", "reflect", "fixture bank", "<the task, verbatim>",
            "--tags", "repo:widgets,scope:platform", "--tags-match", "any_strict", "--budget", "mid",
        ]])

    def test_archivist_renders_writer_instructions_and_arbitration(self):
        prompt = self.render("hindsight.archivist")
        for text in ("gc hindsight ship", "gc hindsight retain", "gc hindsight maintain",
                     "HINDSIGHT_WRITER=archivist", "one at a time in the foreground",
                     "never pass that marker", "deny is the default"):
            self.assertIn(text, prompt)

    def test_coordinator_briefs_and_fragment_gates(self):
        config = self.root / "agents/reader/agent.toml"
        config.write_text('[env]\nHINDSIGHT_MEMORY = "1"\nHINDSIGHT_MENTAL_MODELS = "landmines"\n')
        prompt = self.render("reader")
        self.assertNotIn("gc mail send", prompt)
        command = re.search(r"(?m)^ {4}(gc hindsight read .*?)$", prompt.replace("\\\n", "")).group(1)
        self.run_command("bash", "-eu", "-c", command.replace("<rig>", "widgets"))
        self.assertIn("repo:widgets,scope:platform,scope:business", self.calls()[-1]["args"])
        loop = re.search(r"(?ms)^ {4}TMP=.*?^ {4}done$", prompt).group(0)
        self.assertEqual(self.run_command("bash", "-eu", "-c", loop).stdout, "fixture brief\n")
        self.assertEqual(self.calls()[-1]["args"], [
            "-o", "json", "mental-model", "get", "fixture bank", "landmines",
        ])
        config.write_text("")
        prompt = self.render("reader")
        self.assertIn("# Fixture reader", prompt)
        self.assertNotIn("gc hindsight read", prompt)
        self.assertNotIn("gc mail send", prompt)

    def test_formula_commands_dispatch_and_preserve_requests_and_failures(self):
        formula = tomllib.loads((PACK / "formulas/mol-hindsight-ship.toml").read_text())
        command = re.search(r"(?m)^ {4}(\S.*)$", formula["steps"][0]["description"]).group(1)
        self.mock(self.pack / "assets/scripts/ship-docs.sh")
        self.writer()
        self.run_command("bash", "-eu", "-c", command.replace("{{request}}", "").replace(
            "{{docs_roots}}", "'scheduled docs'"))
        self.assertEqual(self.calls()[-1]["args"], ["--fetch", "--bank", "fixture bank", "scheduled docs"])
        args = ["--bank", "request bank", "--ref", "branch with spaces", "--reprocess",
                str(self.root / "docs with spaces/$(no-execution)")]
        request = base64.b64encode(json.dumps(args).encode()).decode()
        rendered = command.replace("{{request}}", request).replace("{{docs_roots}}", "ignored-default")
        self.run_command("bash", "-eu", "-c", rendered)
        self.assertEqual(self.calls()[-1]["args"], args)
        self.env.pop("HINDSIGHT_WRITER")
        self.run_command("bash", "-eu", "-c", rendered, code=2)
        self.assertEqual(len(self.calls()), 2)

        formula = tomllib.loads((PACK / "formulas/mol-hindsight-consolidate.toml").read_text())
        command = re.search(r"(?m)^ {4}(\S.*)$", formula["steps"][0]["description"]).group(1)
        self.mock(self.pack / "assets/scripts/bank-maintain.sh")
        self.env["MOCK_EXIT"] = "5"
        self.run_command("bash", "-eu", "-c", command, code=5)
        self.assertEqual(self.calls()[-1]["tool"], "bank-maintain.sh")
        self.env.pop("MOCK_EXIT")
        command = re.findall(r"\(`([^`]+)`\)", formula["steps"][1]["description"])[-1].replace("<id>", "landmines")
        self.run_command("bash", "-eu", "-c", command)
        self.assertEqual(self.calls()[-1]["args"], [
            "-o", "json", "mental-model", "get", "fixture bank", "landmines",
        ])


if __name__ == "__main__":
    unittest.main()
