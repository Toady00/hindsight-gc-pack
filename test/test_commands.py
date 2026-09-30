"""Command contracts and real-gc compatibility, without a registered city or services.

Run compatibility alone: python3 test/test_commands.py GasCityCompatibilityTest
GC_TEST_BIN overrides PATH discovery; a missing gc skips only compatibility tests.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest


PACK = Path(__file__).resolve().parents[1]
HELPERS = {
    "lint": "semantic_lint.py",
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
print(json.dumps({'text': 'fixture reflection', 'content': 'fixture brief', 'results': [], 'items': []}))
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

    def run_command(self, *args, code=0, input="", cwd=None):
        result = subprocess.run(
            args, cwd=cwd or self.root, env=self.env, input=input,
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
                    tool="hindsight", args=args if prefix else ["-o", "json", *args],
                    api="https://fixture.invalid", key="fixture-key"))

    def test_captured_reflect_defaults_to_json_and_honors_explicit_formats(self):
        for prefix in ([], ["-o", "pretty"], ["--output", "yaml"], ["-o", "json"]):
            with self.subTest(prefix=prefix):
                args = [*prefix, "memory", "reflect", "fixture bank", "literal $(query); *",
                        "--budget", "mid"]
                result = self.wrapper("read", *args)
                self.assertEqual(self.calls()[-1]["args"], args if prefix else ["-o", "json", *args])
                self.assertEqual(json.loads(result.stdout)["content"], "fixture brief")
                self.assertEqual(result.stderr, "fixture stderr\n")

    def test_terminal_read_keeps_pretty_default(self):
        master, slave = os.openpty()
        try:
            args = ["memory", "reflect", "fixture bank", "fixture query"]
            result = subprocess.run(
                [str(self.pack / "commands/read/run.sh"), *args], env=self.env, cwd=self.root,
                stdout=slave, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.calls()[-1]["args"], args)
        finally:
            os.close(slave)
            os.close(master)

    def test_installed_cli_reflect_has_no_progress_in_captured_output(self):
        binary = shutil.which("hindsight")
        if not binary:
            self.skipTest("Hindsight CLI not installed")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                # Give the old CLI's 80ms spinner time to emit several frames.
                time.sleep(0.3)
                body = json.dumps({"text": "fixture reflection"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            (self.bin / "hindsight").unlink()
            (self.bin / "hindsight").symlink_to(binary)
            self.env["HINDSIGHT_API"] = f"http://127.0.0.1:{server.server_port}"
            result = self.wrapper("read", "memory", "reflect", "fixture", "fixture query")
            self.assertEqual(json.loads(result.stdout)["text"], "fixture reflection")
            self.assertNotIn("\r", result.stdout)
            self.assertNotIn("\x1b", result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_read_rejects_writes_and_unknown_operations_before_dispatch(self):
        for args in (("memory", "retain"), ("-o", "json", "mental-model", "refresh"),
                     ("unknown", "command"), ()):
            with self.subTest(args=args):
                result = self.wrapper("read", *args, code=2)
                self.assertIn("read operations only", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_memory_skill_reflect_extracts_answer_only_on_success(self):
        skill = (PACK / "skills/hindsight-memory/SKILL.md").read_text()
        command = re.search(r"(?ms)^# Task start.*?(?=^# Lookup)", skill).group(0)
        command = command.replace("repo:<your-rig>", "repo:widgets").replace(
            "gc hindsight read", shlex.quote(str(self.pack / "commands/read/run.sh")))
        self.env["BANK"] = "fixture bank"
        result = self.run_command("bash", "-eu", "-c", command)
        self.assertEqual(result.stdout, "fixture reflection\n")
        self.assertEqual(result.stderr, "fixture stderr\n")
        self.assertEqual(self.calls()[-1]["args"], [
            "-o", "json", "memory", "reflect", "fixture bank", "<the task, verbatim>",
            "--tags", "repo:widgets,scope:platform", "--tags-match", "any_strict", "--budget", "mid",
        ])
        self.env["MOCK_EXIT"] = "19"
        result = self.run_command("bash", "-eu", "-c", command, code=19)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "fixture stderr\n")

    def test_published_read_examples_request_json_and_keep_errors_visible(self):
        for path in ("template-fragments/hindsight.template.md", "skills/hindsight-memory/SKILL.md",
                     "skills/hindsight-shipping/SKILL.md", "commands/read/help.md",
                     "formulas/mol-hindsight-consolidate.toml"):
            with self.subTest(path=path):
                text = (PACK / path).read_text()
                commands = re.findall(r"gc hindsight read ([^`\n]+)", text)
                self.assertTrue(commands)
                for command in commands:
                    self.assertTrue(command.startswith("-o json "), command)
                    self.assertNotIn("2>/dev/null", command)
                self.assertNotIn("2>/dev/null", text)

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
        for name in ("pack.toml", "agents", "template-fragments", "formulas"):
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

    def reflect_command(self, prompt):
        command = re.search(
            r"(?m)^ {4}TMP=\$\(mktemp -d\)\n(?: {4}[^\n]*\n)+", prompt).group(0)
        self.assertIn("gc hindsight read -o json memory reflect", command)
        self.assertIn("jq -er '.text'", command)
        self.assertNotIn("2>/dev/null", command)
        return command

    def test_rendered_rig_brief_dispatches_through_real_gc(self):
        prompt = self.render("widgets/reader")
        self.assertIn("# Fixture reader", prompt)
        self.assertIn("gc mail send hindsight.archivist", prompt)
        command = self.reflect_command(prompt)
        result = self.run_command("bash", "-eu", "-c", command)
        self.assertEqual(result.stdout, "fixture reflection\n")
        self.assertEqual(result.stderr, "fixture stderr\n")
        self.assertEqual([c["args"] for c in self.calls()], [[
            "-o", "json", "memory", "reflect", "fixture bank", "<the task, verbatim>",
            "--tags", "repo:widgets,scope:platform", "--tags-match", "any_strict", "--budget", "mid",
        ]])
        self.env["MOCK_EXIT"] = "19"
        result = self.run_command("bash", "-eu", "-c", command, code=19)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "fixture stderr\n")

    def test_semantic_lint_preview_dispatches_without_services_or_key(self):
        shutil.copytree(PACK / "schemas", self.pack / "schemas")
        path = self.root / "doc with spaces.md"
        path.write_text((PACK / "test/docs/spec.eventing.transport.0001.md").read_text())
        self.env.pop("TYPESAFE_API_KEY", None)
        result = self.run_command(self.gc, "hindsight", "lint", "--dry-run", str(path))
        report = json.loads(result.stdout)
        self.assertEqual(report["documents"][0]["status"], "preview")
        self.assertIn("accepted-draft", report["documents"][0]["request"]["questions"])
        self.assertIn("state", report["documents"][0]["request"])
        self.assertEqual(self.calls(), [])

    def test_semantic_lint_from_rig_preserves_relative_document_paths(self):
        shutil.copytree(PACK / "schemas", self.pack / "schemas")
        self.env.pop("TYPESAFE_API_KEY", None)
        for key in ("GC_CITY", "GC_CITY_PATH", "GC_CITY_ROOT"):
            self.env.pop(key, None)
        with tempfile.TemporaryDirectory(prefix="hindsight-external-rig-") as external:
            for rig, city in ((self.root / "widgets", None), (Path(external), str(self.root))):
                with self.subTest(rig=rig, city=city):
                    if city:
                        self.env["GC_CITY"] = city
                    path = rig / "doc with spaces.md"
                    path.write_text((PACK / "test/docs/spec.eventing.transport.0001.md").read_text())
                    result = self.run_command(self.gc, "hindsight", "lint", "--dry-run", path.name, cwd=rig)
                    report = json.loads(result.stdout)
                    self.assertEqual(report["documents"][0]["status"], "preview")
                    self.assertEqual(report["documents"][0]["path"], path.name)
        self.assertEqual(self.calls(), [])

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
        command = self.reflect_command(prompt).replace("<rig>", "widgets")
        result = self.run_command("bash", "-eu", "-c", command)
        self.assertEqual(result.stdout, "fixture reflection\n")
        self.assertEqual(result.stderr, "fixture stderr\n")
        self.assertEqual(self.calls()[-1]["args"], [
            "-o", "json", "memory", "reflect", "fixture bank", "<the ask, verbatim>",
            "--tags", "repo:widgets,scope:platform,scope:business",
            "--tags-match", "any_strict", "--budget", "mid",
        ])
        loop = re.search(r"(?ms)^ {4}TMP=\$\(mktemp -d\)\n {4}for m .*?^ {4}done$", prompt).group(0)
        result = self.run_command("bash", "-eu", "-c", loop)
        self.assertEqual(result.stdout, "fixture brief\n")
        self.assertEqual(result.stderr, "fixture stderr\n")
        self.assertEqual(self.calls()[-1]["args"], [
            "-o", "json", "mental-model", "get", "fixture bank", "landmines",
        ])
        self.env["MOCK_EXIT"] = "19"
        for script in (command, loop):
            result = self.run_command("bash", "-eu", "-c", script, code=19)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "fixture stderr\n")
        config.write_text("")
        prompt = self.render("reader")
        self.assertIn("# Fixture reader", prompt)
        self.assertNotIn("gc hindsight read", prompt)
        self.assertNotIn("gc mail send", prompt)

    def test_vapor_roots_preserve_complete_ordered_workflows(self):
        for name in ("mol-hindsight-ship", "mol-hindsight-consolidate"):
            with self.subTest(formula=name):
                source = tomllib.loads((PACK / "formulas" / f"{name}.toml").read_text())
                args = [self.gc, "--city", str(self.root), "formula", "show", name, "--json"]
                if name == "mol-hindsight-ship":
                    args += ["--var", "request=fixture-request"]
                else:
                    args += ["--var", "audit_models=fixture-model"]
                compiled = json.loads(self.run_command(*args).stdout)
                self.assertEqual(source["phase"], "vapor")
                self.assertFalse(source.get("pour", False))
                self.assertFalse(source.get("steps"))
                self.assertEqual(len(compiled["steps"]), 1)
                self.assertFalse(compiled.get("deps"))
                root = compiled["steps"][0]
                self.assertEqual(root["id"], name)
                self.assertEqual(root["type"], "task")
                description = root["description"]
                self.assertNotIn("{{", description)
                self.assertIn("notes before closing this root", description)
                if name == "mol-hindsight-ship":
                    self.assertEqual(re.findall(r"(?m)^## (.*)$", description), ["1. Ship sync", "2. Report"])
                    self.assertIn("gc hindsight ship --request 'fixture-request'", description)
                    for text in ("last_success receipts", "extraction child results",
                                 "Report confirmed document completion separately",
                                 "Do not repeat --reprocess", "ordinary scan"):
                        self.assertIn(text, description)
                else:
                    self.assertEqual(re.findall(r"(?m)^## (.*)$", description),
                                     ["1. Maintain", "2. Model audit", "3. Report"])
                    for text in ("gc hindsight maintain", "unless maintain exited 0 or 2",
                                 "fixture-model", 'gc hindsight read -o json mental-model get',
                                 "Mail the mayor", "drain/consolidate/audit_findings"):
                        self.assertIn(text, description)

    def test_formula_commands_dispatch_and_preserve_requests_and_failures(self):
        formula = tomllib.loads((PACK / "formulas/mol-hindsight-ship.toml").read_text())
        command = re.search(r"(?m)^ {4}(\S.*)$", formula["description"]).group(1)
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
        command = re.search(r"(?m)^ {4}(\S.*)$", formula["description"]).group(1)
        self.mock(self.pack / "assets/scripts/bank-maintain.sh")
        self.env["MOCK_EXIT"] = "5"
        self.run_command("bash", "-eu", "-c", command, code=5)
        self.assertEqual(self.calls()[-1]["tool"], "bank-maintain.sh")
        self.env.pop("MOCK_EXIT")
        command = re.findall(r"\(`([^`]+)`\)", formula["description"])[-1].replace("<id>", "landmines")
        self.run_command("bash", "-eu", "-c", command)
        self.assertEqual(self.calls()[-1]["args"], [
            "-o", "json", "mental-model", "get", "fixture bank", "landmines",
        ])


if __name__ == "__main__":
    unittest.main()
