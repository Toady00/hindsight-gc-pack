"""Opt-in installed-gc lifecycle test, with no Hindsight or model connections.

HINDSIGHT_RUNTIME_TEST=1 python3 -m unittest test.test_archivist_runtime -v

Uses real supervisor, subprocess sessions, routing, Ready and hook claims. The
deterministic provider interprets phase commands, not natural-language decisions.
Only ship-docs.sh, bank-maintain.sh and the hindsight CLI are mocked. Beads uses
a private loopback Dolt server. No bd shim, direct store writes, session start,
or parent-side hook claim is used.
"""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import urllib.request


PACK = Path(__file__).resolve().parents[1]
FIXTURE = PACK / "test/fixtures/archivist_runtime.py"
TARGET = "hindsight.archivist"


def health(base):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(base + "/health", timeout=5) as response:
        return json.load(response)


@unittest.skipUnless(os.environ.get("HINDSIGHT_RUNTIME_TEST") == "1",
                     "set HINDSIGHT_RUNTIME_TEST=1 to start an isolated gc supervisor")
class ArchivistRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.gc = shutil.which(os.environ.get("GC_TEST_BIN", "gc"))
        self.assertIsNotNone(self.gc, "installed gc required")
        # Short paths are required for Darwin's Unix socket path limit.
        self.temp = tempfile.TemporaryDirectory(prefix="hsrt-", dir="/tmp")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.city = self.root / "city"
        self.pack = self.root / "pack"
        for directory in ("city/.gc", "home", "gc", "run", "bin"):
            (self.root / directory).mkdir(parents=True)
        for name in ("pack.toml", "agents", "template-fragments", "formulas", "commands", "assets/scripts"):
            source = PACK / name
            if source.is_dir():
                shutil.copytree(source, self.pack / name, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                self.pack.mkdir(exist_ok=True)
                shutil.copy2(source, self.pack / name)
        # Allowlist, rather than scrub: never inherit provider, Hindsight, Beads,
        # remote gc, proxy, or live-city connection credentials.
        self.env = dict(
            PATH=f"{self.root / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            HOME=str(self.root / "home"), GC_HOME=str(self.root / "gc"),
            XDG_RUNTIME_DIR=str(self.root / "run"), XDG_CONFIG_HOME=str(self.root / "home/.config"),
            TMPDIR=str(self.root), GC_CITY=str(self.city), GC_CITY_PATH=str(self.city),
            GC_CITY_ROOT=str(self.city), GC_SESSION="subprocess", GC_BEADS="bd",
            GC_DISABLE_USAGE_METRICS="1", GC_SUPERVISOR_LOG_TEE="0",
            HINDSIGHT_BANK="runtime-fixture", HINDSIGHT_API="http://127.0.0.1:1",
            HS_RUNTIME_ROOT=str(self.root), PYTHONUNBUFFERED="1", LANG="en_US.UTF-8")
        tools = {name: shutil.which(name) for name in ("bd", "dolt", "jq", "bash")}
        self.assertTrue(all(tools.values()), f"required installed tools: {tools}")
        for name, binary in dict(gc=self.gc, python3=sys.executable, **tools).items():
            (self.root / "bin" / name).symlink_to(binary)
        bash_check = subprocess.run(
            [tools["bash"], "-uc", 'a=(); : "${a[@]}"'], env=self.env,
            capture_output=True, text=True, timeout=5)
        self.assertEqual(bash_check.returncode, 0, "Bash 4.4+ required for nounset-safe empty arrays")
        self.assertEqual(self.run_gc("version").stdout.strip(), "1.4.2+local.4eb766c0b",
                         "Recheck the runtime contract before changing this pin")
        for name in ("curl", "claude", "codex", "hindsight"):
            self.script(self.root / "bin" / name, "boundary")
        for name in ("ship-docs.sh", "bank-maintain.sh"):
            self.script(self.pack / "assets/scripts" / name, "boundary")
        self.script(self.root / "worker", "worker")
        (self.city / "pack.toml").write_text(
            '[pack]\nname = "runtime-fixture"\nschema = 2\n'
            '[imports.hindsight]\nsource = ' + json.dumps(str(self.pack)) + '\n')
        (self.city / "city.toml").write_text(
            '[workspace]\nprovider = "fixture"\n'
            '[providers.fixture]\ncommand = ' + json.dumps(str(self.root / "worker")) + '\n'
            'prompt_mode = "none"\n[beads]\nprovider = "bd"\n'
            '[session]\nprovider = "subprocess"\n'
            '[daemon]\npatrol_interval = "5s"\n')
        (self.city / ".gc/site.toml").write_text('workspace_name = "runtime-fixture"\n')
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dolt_port = sock.getsockname()[1]
        with (self.city / "city.toml").open("a") as config:
            config.write(f'\n[dolt]\nhost = "127.0.0.1"\nport = {dolt_port}\n')
        self.env.update(GC_DOLT_HOST="127.0.0.1", GC_DOLT_PORT=str(dolt_port),
                        BEADS_DOLT_SERVER_HOST="127.0.0.1", BEADS_DOLT_SERVER_PORT=str(dolt_port),
                        BEADS_DIR=str(self.city / ".beads"), BD_NON_INTERACTIVE="1",
                        DOLT_CLI_NO_METRICS="true", DOLT_AUTHOR_NAME="Runtime Fixture",
                        DOLT_AUTHOR_EMAIL="fixture@example.invalid")
        (self.root / "dolt").mkdir()
        dolt_log = (self.root / "dolt.log").open("w")
        self.addCleanup(dolt_log.close)
        self.dolt = subprocess.Popen(
            [tools["dolt"], "sql-server", "--host", "127.0.0.1", "--port", str(dolt_port),
             "--data-dir", str(self.root / "dolt"), "--socket", str(self.root / "dolt.sock")],
            cwd=self.root / "dolt", env=self.env, stdout=dolt_log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop_dolt)
        for _ in range(100):
            self.assertIsNone(self.dolt.poll(), (self.root / "dolt.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", dolt_port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            self.fail("private Dolt did not listen\n" + (self.root / "dolt.log").read_text())
        initialized = subprocess.run(
            [tools["bd"], "init", "--server", "--external", "--server-host", "127.0.0.1",
             "--server-port", str(dolt_port), "--prefix", "rt", "--skip-agents", "--skip-hooks"],
            cwd=self.city, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        self.run_gc("beads", "city", "use-external", "--host", "127.0.0.1", "--port", str(dolt_port))
        self.run_gc("bd", "config", "set", "types.custom",
                    "molecule,convoy,message,event,gate,merge-request,agent,role,rig,session,spec,convergence,step")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        (self.root / "gc/supervisor.toml").write_text(f"[supervisor]\nport = {port}\n")
        self.log = (self.root / "supervisor.log").open("w")
        self.addCleanup(self.log.close)
        self.supervisor = subprocess.Popen(
            [self.gc, "supervisor", "run"], cwd=self.city, env=self.env,
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.addCleanup(self.stop)
        self.wait(lambda: health(self.base), "supervisor health")

    def script(self, path, mode):
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" {mode} "{path}" "$@"\n')
        path.chmod(0o755)

    def run_gc(self, *args):
        result = subprocess.run([self.gc, *args], cwd=self.city, env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, f"{args}\n{result.stdout}\n{result.stderr}")
        return result

    def stop(self):
        # Stop only this registered city and this foreground supervisor.
        try:
            stopped = subprocess.run([self.gc, "stop", str(self.city)], cwd=self.city, env=self.env,
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        finally:
            if self.supervisor.poll() is None:
                self.supervisor.terminate()
                try:
                    self.supervisor.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(self.supervisor.pid, signal.SIGKILL)
                    self.supervisor.wait(timeout=5)
            # A reset may orphan a foreground tool from the old provider. Only
            # reap recorded fixture processes whose argv still names this city.
            path = self.root / "processes.jsonl"
            if path.exists():
                for line in path.read_text().splitlines():
                    pid = json.loads(line)["pid"]
                    probe = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
                    if str(self.root) not in probe.stdout or str(FIXTURE) not in probe.stdout:
                        continue
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        continue
                time.sleep(.2)

    def stop_dolt(self):
        if self.dolt.poll() is None:
            self.dolt.terminate()
            try:
                self.dolt.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.dolt.kill()
                self.dolt.wait(timeout=5)

    def events(self):
        path = self.root / "worker.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait(self, predicate, what):
        deadline = time.monotonic() + 65
        last = None
        while time.monotonic() < deadline:
            errors = [e for e in self.events() if e["event"] == "error"]
            if errors:
                self.fail(str(errors) + "\n" + (self.root / "supervisor.log").read_text())
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError) as exc:
                last = exc
            if self.supervisor.poll() is not None:
                break
            time.sleep(.1)
        self.fail(f"Timed out: {what}; {last}\nworker={self.events()}\n"
                  + (self.root / "supervisor.log").read_text())

    def workflow_beads(self, formula):
        return json.loads(self.run_gc(
            "bd", "list", "--all", "--limit", "0", "--json",
            "--metadata-field", f"gc.formula_name={formula}").stdout)

    def assert_root_only(self, formula, root_id):
        self.assertEqual([bead["id"] for bead in self.workflow_beads(formula)], [root_id])
        children = json.loads(self.run_gc(
            "bd", "list", "--all", "--limit", "0", "--json", "--parent", root_id).stdout)
        self.assertEqual(children, [], "instantiated root acquired child beads")

    def lifecycle(self, workflow, code, scheduled=False, audit_fault="", arrive_after_idle=False):
        formula = "mol-hindsight-ship" if workflow == "ship" else "mol-hindsight-consolidate"
        source = tomllib.loads((self.pack / "formulas" / f"{formula}.toml").read_text())
        self.assertEqual(source["phase"], "vapor")
        self.assertFalse(source.get("pour", False), "root-only workflow must not pour")
        self.assertFalse(source.get("steps"), "workflow must live in the root description")
        headings = ["1. Ship sync", "2. Report"] if workflow == "ship" else [
            "1. Maintain", "2. Model audit", "3. Report"]
        self.assertEqual(re.findall(r"^## (.+)$", source["description"], re.M), headings)
        self.assertRegex(source["description"].lower(), r"close.{0,80}root|root.{0,80}clos")
        self.assertIn("Close as the claimed root's confirmed assignee using --actor, never --force.",
                      " ".join(source["description"].split()))
        compiled = json.loads(self.run_gc("formula", "show", formula, "--json").stdout)
        self.assertEqual([step["id"] for step in compiled["steps"]], [formula])
        self.assertFalse(compiled.get("deps"))
        (self.root / "exit-code").write_text(str(code))
        (self.root / "audit-fault").write_text(audit_fault)
        if arrive_after_idle:
            self.assertEqual((workflow, scheduled), ("ship", False))
            self.observe_idle_city()
            self.assertEqual(self.workflow_beads(formula), [])
        # The worker pauses before claiming until we have observed Ready. This
        # controls the test race, not wake: only the controller starts workers.
        if workflow == "ship" and not scheduled:
            self.run_gc("hindsight", "ship", "--bank", "runtime-fixture", "--reprocess",
                        str(self.city / "docs with spaces"))
        else:
            # Preserve the pack order's formula, target and cooldown trigger.
            (self.pack / "orders").mkdir()
            order_file = "ship-sync.toml" if workflow == "ship" else "consolidate.toml"
            shutil.copy2(PACK / "orders" / order_file, self.pack / "orders" / order_file)
        if not arrive_after_idle:
            self.assertEqual(self.events(), [], "provider ran before a controller existed")
            self.register_fixture()

        def ready_roots():
            ready = json.loads(self.run_gc("ready", "--json").stdout)
            return [b for b in ready if b.get("metadata", {}).get("gc.formula_name") == formula
                    and b.get("metadata", {}).get("gc.routed_to") == TARGET]

        roots = self.wait(ready_roots, "scheduled order root in Ready")
        self.assertEqual(len(roots), 1, roots)
        root = roots[0]
        self.assertEqual(root["status"], "open")
        self.assertEqual(root["issue_type"], "task")
        self.assertFalse(root.get("assignee"))
        self.assertEqual(root["metadata"]["gc.routed_to"], TARGET)
        self.assert_root_only(formula, root["id"])
        # Compare the actual bd-ready predicate used by the default work query,
        # not an emulated file-store readiness decision.
        bd_ready = json.loads(self.run_gc(
            "bd", "ready", "--include-ephemeral", "--metadata-field", f"gc.routed_to={TARGET}",
            "--unassigned", "--exclude-type=epic", "--exclude-label", "hold:mayor", "--json").stdout)
        self.assertEqual([bead["id"] for bead in bd_ready], [root["id"]])
        (self.root / "claim-allowed").touch()
        self.wait(lambda: any(e["event"] == "closed" for e in self.events()), "root completion")
        events = self.events()
        audit_events = (["audit_failed"] if audit_fault else
                        ["audit", "audit"] if workflow == "maintain" and code in (0, 2) else [])
        self.assertEqual([e["event"] for e in events],
                         ["wake", "claim", "execute", *audit_events,
                          "report", "closed"], events)
        self.assertEqual(events[1]["bead_id"], root["id"])
        self.assertTrue(events[0]["session_id"])
        self.assertEqual(events[0]["spawn_origin"], "demand")
        self.assertEqual(events[0]["session_origin"], "ephemeral")
        controller_log = (self.root / "supervisor.log").read_text()
        self.assertIn("poolDesired: hindsight.archivist = 1", controller_log)
        self.assertIn("Woke session 'hindsight.archivist'", controller_log)
        shown = json.loads(self.run_gc("bd", "show", root["id"], "--json").stdout)
        self.assertEqual(shown[0]["status"], "closed")
        self.assert_root_only(formula, root["id"])
        report = json.loads(shown[0]["notes"])
        self.assertEqual(report["exit_code"], code, report)
        outcome = ("blocked" if audit_fault else "clean" if code == 0 else
                   "findings" if workflow == "maintain" and code == 2 else "blocked")
        self.assertEqual(report["outcome"], outcome)
        self.assertEqual(report["incomplete"], outcome == "blocked")
        self.assertEqual(shown[0]["close_reason"], report["close_reason"])
        self.assertIn("incomplete" if outcome == "blocked" else outcome, shown[0]["close_reason"].lower())
        self.assertIn(f"exit {code}", shown[0]["close_reason"])
        if outcome == "blocked":
            self.assertIn("shipping" if workflow == "ship" else "maintenance", shown[0]["close_reason"])
        self.assertEqual(events[-1]["reason"], shown[0]["close_reason"])
        self.assertIn("fixture", report["output"])
        expected_audit = ("incomplete" if audit_fault else
                          "clean" if workflow == "maintain" and code in (0, 2) else "skipped")
        self.assertEqual(report["model_audit"], expected_audit)
        if audit_fault:
            self.assertEqual(len(report["audit_errors"]), 1)
            diagnostic = report["audit_errors"][0]
            self.assertEqual(diagnostic["model"], "conventions-and-standards")
            self.assertIn("gc hindsight read", diagnostic["command"])
            expected_error = "read exit 5: fixture model read unavailable" if audit_fault == "read-error" else "JSONDecodeError"
            self.assertIn(expected_error, diagnostic["error"])
            self.assertIn(diagnostic["error"], shown[0]["close_reason"])
            self.assertIn("model audit incomplete", shown[0]["close_reason"])
        else:
            self.assertEqual(report["audit_errors"], [])
        calls = [json.loads(line) for line in (self.root / "boundary.jsonl").read_text().splitlines()]
        self.assertEqual(calls[0]["name"], "ship-docs.sh" if workflow == "ship" else "bank-maintain.sh")
        if workflow == "ship":
            expected_args = (["--fetch", "--bank", "runtime-fixture"] if scheduled else
                             ["--api", "http://127.0.0.1:1", "--bank", "runtime-fixture",
                              "--reprocess", str(self.city / "docs with spaces")])
            self.assertEqual(calls[0]["args"], expected_args)
            for text in ("last_success receipts", "extraction child results", "Do not repeat --reprocess", "ordinary scan"):
                self.assertIn(text, root["description"])
        self.assertEqual(len(calls), 2 if audit_fault else 3 if workflow == "maintain" and code in (0, 2) else 1)

    def register_fixture(self):
        # `gc register` invokes platform supervisor start, which deliberately
        # rejects a temporary HOME even when a foreground supervisor exists.
        # Seed only our isolated registry; its real reconcile loop loads it.
        (self.root / "gc/cities.toml").write_text(
            '[[cities]]\nname = "runtime-fixture"\npath = ' + json.dumps(str(self.city)) + '\n')

    def observe_idle_city(self):
        self.register_fixture()
        self.wait(lambda: "City started." in (self.root / "supervisor.log").read_text(), "idle city start")
        time.sleep(11)  # Observe two complete 5s patrol intervals.
        self.assertEqual(self.events(), [])
        self.assertIsNone(self.supervisor.poll())
        log = (self.root / "supervisor.log").read_text()
        self.assertNotIn("Woke session", log)
        self.assertNotRegex(log, r"(?:poolDesired|scaleCheck): hindsight\.archivist = [1-9]")

    def test_no_demand_does_not_wake_archivist(self):
        self.observe_idle_city()

    def test_two_queued_workflows_continue_without_nudges_or_fresh_cycle(self):
        self.seed_dormant_named_session()
        (self.root / "exit-code").write_text("0")
        (self.root / "audit-fault").write_text("")
        (self.root / "queue-test").touch()
        self.run_gc("sling", TARGET, "mol-hindsight-consolidate", "--formula")
        maintenance = self.workflow_beads("mol-hindsight-consolidate")[0]
        self.run_gc("bd", "update", maintenance["id"], "--priority", "1")
        self.run_gc("hindsight", "ship", "--bank", "runtime-fixture", str(self.city / "docs"))
        shipping = self.workflow_beads("mol-hindsight-ship")[0]
        (self.root / "claim-allowed").touch()
        self.register_fixture()
        self.wait(lambda: len([e for e in self.events() if e["event"] == "closed"]) == 2,
                  "both queued workflows close without external intervention")
        events = self.events()
        wakes = [e for e in events if e["event"] == "wake"]
        self.assertEqual(len(wakes), 1,
                         (self.root / "supervisor.log").read_text() + "\n" +
                         self.run_gc("bd", "show", wakes[0]["session_id"], "--json").stdout)
        self.assertEqual(events[0]["session_origin"], "named")
        self.assertEqual([e["bead_id"] for e in events if e["event"] == "claim"],
                         [maintenance["id"], shipping["id"]])
        self.assertEqual([e["event"] for e in events],
                         ["wake", "claim", "execute", "audit", "audit", "report", "closed",
                          "claim", "execute", "report", "closed"])
        for root in (maintenance, shipping):
            stored = json.loads(self.run_gc("bd", "show", root["id"], "--json").stdout)[0]
            self.assertEqual(stored["status"], "closed")
            self.assertEqual(json.loads(stored["notes"])["outcome"], "clean")
        self.assertNotIn("Cycled fresh-mode session", (self.root / "supervisor.log").read_text())

    def seed_dormant_named_session(self, wake_mode="resume"):
        # Model the existing city: a previously created named identity is asleep.
        # This creates only its durable record, never a provider process.
        metadata = dict(template=TARGET, agent_name=TARGET, alias=TARGET,
                        session_name="hindsight__archivist", session_origin="named",
                        configured_named_identity=TARGET, configured_named_session="true",
                        configured_named_mode="on_demand", state="asleep", sleep_reason="idle-timeout",
                        provider="fixture", wake_mode=wake_mode, work_dir=str(self.city / ".gc/agents/archivist"))
        self.run_gc("bd", "create", "--type", "session", "--title", TARGET, "--status", "open",
                    "--label", "gc:session,template:" + TARGET, "--metadata", json.dumps(metadata), "--json")

    def test_reset_after_receipt_preserves_success_in_zero_write_report(self):
        self.seed_dormant_named_session()
        (self.root / "exit-code").write_text("0")
        (self.root / "audit-fault").write_text("")
        (self.root / "reset-shipping").touch()
        self.run_gc("hindsight", "ship", "--bank", "runtime-fixture", str(self.city / "docs"))
        root = self.workflow_beads("mol-hindsight-ship")[0]
        (self.root / "claim-allowed").touch()
        self.register_fixture()
        self.wait(lambda: (self.root / "reset-ready").exists(), "durable receipt before reset")
        session_id = next(e["session_id"] for e in self.events() if e["event"] == "wake")
        self.run_gc("session", "reset", session_id, "--json")
        self.wait(lambda: any(e["event"] == "closed" for e in self.events()), "resumed report and closure")
        events = self.events()
        self.assertGreaterEqual(len([e for e in events if e["event"] == "wake"]), 2)
        posts = [json.loads(line) for line in (self.root / "retain-posts.jsonl").read_text().splitlines()]
        self.assertEqual(len(posts), 1, "reset must not duplicate confirmed writes")
        stored = json.loads(self.run_gc("bd", "show", root["id"], "--json").stdout)[0]
        self.assertEqual(stored["status"], "closed")
        report = json.loads(stored["notes"])
        self.assertEqual(report["outcome"], "blocked")
        evidence = report["receipt_report"]
        self.assertEqual(evidence["latest_run"]["counts"]["shipped"], 0)
        self.assertEqual([r["document_id"] for r in evidence["document_receipts"]], ["doc-a"])
        self.assertEqual(evidence["document_receipts"][0]["work_id"], root["id"])
        self.assertEqual(evidence["document_receipts"][0]["operation_id"], posts[0]["operation_id"])
        self.assertEqual(evidence["unresolved_documents"], [])
        self.assertFalse(evidence["healthy"])

    def test_ship_request(self):
        self.lifecycle("ship", 0, arrive_after_idle=True)

    def test_scheduled_ship_order(self):
        self.lifecycle("ship", 0, scheduled=True)

    def test_ship_bank_drain_timeout_still_reports(self):
        self.lifecycle("ship", 3)

    def test_maintenance_order(self):
        self.lifecycle("maintain", 0)

    def test_maintenance_findings_still_audits_models(self):
        self.lifecycle("maintain", 2)

    def test_maintenance_inventory_error_skips_models_and_reports(self):
        self.lifecycle("maintain", 5)

    def test_model_read_failure_reports_incomplete_and_closes(self):
        self.lifecycle("maintain", 0, audit_fault="read-error")

    def test_model_malformed_json_reports_incomplete_and_closes(self):
        self.lifecycle("maintain", 0, audit_fault="malformed-json")

    def test_legacy_poured_children_do_not_generate_controller_demand(self):
        formula = "mol-runtime-legacy"
        (self.pack / "formulas" / f"{formula}.toml").write_text(
            'description = "Legacy root with work only in its child."\n'
            f'formula = "{formula}"\nversion = "0.1.0"\nphase = "vapor"\npour = true\n'
            '[[steps]]\nid = "execute"\ntitle = "Legacy executable child"\n'
            'description = "Run gc hindsight maintain, report in notes, then close."\n')
        # Deliberately use the real instantiation/routing path, not the positive
        # test's source-shape guard. Both beads must exist before checking Ready.
        self.run_gc("sling", TARGET, formula, "--formula")
        beads = json.loads(self.run_gc("bd", "list", "--all", "--limit", "0", "--json").stdout)
        self.assertEqual(len(beads), 2, beads)
        roots = [bead for bead in beads if bead["issue_type"] == "molecule"]
        self.assertEqual(len(roots), 1, beads)
        root = roots[0]
        self.assertEqual(root["metadata"]["gc.routed_to"], TARGET)
        children = json.loads(self.run_gc(
            "bd", "list", "--all", "--limit", "0", "--json", "--parent", root["id"]).stdout)
        self.assertEqual(len(children), 1, children)
        self.assertEqual({bead["id"] for bead in beads}, {root["id"], children[0]["id"]})

        def assert_ineligible():
            visible = json.loads(self.run_gc("bd", "ready", "--include-ephemeral", "--json").stdout)
            self.assertIn(children[0]["id"], [bead["id"] for bead in visible])
            controller_ready = json.loads(self.run_gc("ready", "--json").stdout)
            self.assertFalse({bead["id"] for bead in beads} & {bead["id"] for bead in controller_ready})
            self.assertEqual(self.events(), [], "legacy poured work woke a provider")
            self.assertIsNone(self.supervisor.poll())

        assert_ineligible()
        self.register_fixture()
        self.wait(lambda: "City started." in (self.root / "supervisor.log").read_text(), "negative-control city start")
        # Inspect at both ends of two patrol intervals, without claiming or
        # releasing the provider barrier even if the regression starts a worker.
        for _ in range(2):
            time.sleep(5.5)
            assert_ineligible()
        log = (self.root / "supervisor.log").read_text()
        self.assertNotRegex(log, r"(?:poolDesired|scaleCheck): hindsight\.archivist = [1-9]")
        self.assertNotIn("Woke session", log)
        self.assertFalse((self.root / "boundary.jsonl").exists())
        for bead in beads:
            stored = json.loads(self.run_gc("bd", "show", bead["id"], "--json").stdout)[0]
            self.assertEqual(stored["status"], "open")
            self.assertFalse(stored.get("assignee"))


if __name__ == "__main__":
    unittest.main()
