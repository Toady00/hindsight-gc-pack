"""Offline memory retries through the real shell, CLI, and ingestion library."""

import hashlib
import json
from pathlib import Path
import unittest

try:
    from . import test_pack
except ImportError:
    import test_pack


SCRIPTS = test_pack.SCRIPTS
DOCUMENT_ID = "gotcha.fixture"


class MemoryRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.pack = test_pack.PackTest()
        self.addCleanup(self.pack.doCleanups)
        self.pack.setUp()
        # Inject failures only in this suite's disposable service executables.
        mock = test_pack.MOCK.replace(
            "        db_path.write_text(json.dumps(db))",
            "        if (config.get('completion_write_error') and "
            "entry.get('payload', {}).get('hindsight', {}).get('data', {})"
            ".get('attempt', {}).get('state') == 'succeeded'): sys.exit(1)\n"
            "        db_path.write_text(json.dumps(db))",
        ).replace(
            "        save_server()\n        print(json.dumps(result))",
            "        save_server()\n"
            "        if config.get('lost_ack'): sys.exit(22)\n"
            "        print(json.dumps(result))",
        )
        for name in ("gc", "curl"):
            (self.pack.bin / name).write_text(mock)

    def memory(self, *args, **kwargs):
        return self.pack.run_script(SCRIPTS / "memory-retain.sh", "--id", DOCUMENT_ID, *args, **kwargs)

    def fresh(self, *args, **kwargs):
        return self.memory("--title", "Fixture", "--repos", "repo", *args,
                           input="Symptom, cause, fix.", **kwargs)

    def state(self):
        return self.pack.document_state(DOCUMENT_ID)

    def stored_document(self):
        return json.loads(self.pack.server.read_text())["documents"][0]

    def test_fresh_receipt_exact_hash_connection_and_output(self):
        p = self.pack
        p.env.update(HINDSIGHT_API="https://unused.invalid", HINDSIGHT_API_KEY="fixture-secret")
        result = self.fresh("--api", "https://override.invalid/", "--bank", "bank /name")
        item = p.payload()
        state = self.state()
        expected = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()
        self.assertEqual(state["source"], {"kind": "agent-memory"})
        self.assertEqual(state["last_success"]["source_hash"], expected)
        self.assertEqual(state["attempt"]["state"], "succeeded")
        self.assertNotIn("payload", state["attempt"])
        self.assertEqual(item["metadata"]["hit_count"], "1")
        self.assertIn("scope:repo", item["tags"])
        self.assertIn("status:accepted", item["tags"])
        self.assertIn(f"RETAINED {DOCUMENT_ID} op=", result.stdout)
        self.assertIn("hit_count=1", result.stdout)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        record = p.bead("document", DOCUMENT_ID)
        self.assertEqual(record["status"], "pinned")
        self.assertEqual(record["metadata"]["hindsight"]["api"], "https://override.invalid")
        self.assertEqual(record["metadata"]["hindsight"]["bank"], "bank /name")
        for call in p.calls("gc"):
            self.assertEqual(call["args"][:3], ["bd", "--city", str(p.repo)])
        for call in p.calls("curl"):
            self.assertEqual(call["api"], "https://override.invalid")
            self.assertIn("Authorization: Bearer fixture-secret", call["stdin"])
            self.assertNotIn("fixture-secret", str(call["args"]))
        self.assertFalse(Path(p.env["HINDSIGHT_STATE_DIR"]).exists())

    def test_completion_write_failure_recovers_before_bump_read(self):
        p = self.pack
        p.config["completion_write_error"] = True
        self.fresh(code=5)
        original = self.state()["attempt"]
        self.assertEqual(original["state"], "pending")
        self.assertEqual(self.stored_document()["document_metadata"]["hit_count"], "1")
        self.assertNotIn("last_success", self.state())
        p.config.clear()
        before = len(p.calls())
        # No mock document response is supplied: a premature bump read would fail.
        result = self.memory("--bump", "--content-file", p.root / "must-not-read")
        self.assertIn(f"RECOVERED {DOCUMENT_ID}", result.stdout)
        self.assertIn("prior attempt completed; no additional hit counted; run again for a separate report",
                      result.stdout)
        self.assertEqual(len(p.posts()), 1)
        self.assertFalse(p.calls("hindsight"))
        self.assertEqual(self.state()["last_success"]["operation_id"], original["operation_id"])
        calls = p.calls()[before:]
        self.assertEqual([c["tool"] for c in calls[:3]], ["curl", "curl", "gc"])
        self.assertTrue(all("/operations?" in " ".join(c["args"]) for c in calls[:2]))

        p.config["document"] = self.stored_document()
        result = self.memory("--bump")
        self.assertIn("hit_count=2", result.stdout)
        self.assertEqual(len(p.posts()), 2)
        self.assertEqual(p.payload()["metadata"]["hit_count"], "2")
        self.assertNotEqual(self.state()["last_success"]["operation_id"], original["operation_id"])

    def test_lost_ack_recovers_without_read_or_resubmit(self):
        p = self.pack
        p.config["lost_ack"] = True
        self.fresh(code=5)
        operation_id = self.state()["attempt"]["operation_id"]
        p.config.clear()
        result = self.memory("--bump")
        self.assertIn("RECOVERED", result.stdout)
        self.assertFalse(p.calls("hindsight"))
        self.assertEqual(len(p.posts()), 1)
        self.assertEqual(self.state()["last_success"]["operation_id"], operation_id)

    def test_unknown_attempt_replays_exact_payload_before_new_content(self):
        p = self.pack
        p.config["post_error"] = True
        self.fresh(code=5)
        original = p.posts()[0]["payload"]
        p.config.clear()
        result = self.memory("--title", "Different report", input="Do not retain this yet.")
        self.assertIn("RECOVERED", result.stdout)
        self.assertEqual(len(p.posts()), 2)
        self.assertEqual(p.posts()[1]["payload"], original)
        self.assertEqual(self.stored_document()["content"], original["items"][0]["content"])

    def test_failed_partial_retain_retries_original_operation_only(self):
        p = self.pack
        p.config["operation_status"] = "failed"
        self.fresh(code=1)
        operation_id = self.state()["attempt"]["operation_id"]
        self.assertEqual(self.stored_document()["document_metadata"]["hit_count"], "1")
        p.config.clear()
        result = self.memory("--bump")
        self.assertIn("RECOVERED", result.stdout)
        self.assertEqual(len(p.posts()), 2)
        self.assertTrue(any(a.endswith(f"/operations/{operation_id}/retry") for a in p.posts()[-1]["args"]))
        self.assertFalse(p.calls("hindsight"))

    def test_recovery_error_stops_before_bump_and_preserves_attempt(self):
        p = self.pack
        p.config["lost_ack"] = True
        self.fresh(code=5)
        original = self.state()
        p.config = {"poll_error": True}
        result = self.memory("--bump", code=5)
        self.assertNotIn("RECOVERED", result.stdout)
        self.assertFalse(p.calls("hindsight"))
        self.assertEqual(self.state(), original)
        self.assertEqual(len(p.posts()), 1)

    def test_idle_marker_and_identical_deliberate_retains_are_not_suppressed(self):
        p = self.pack
        cli = SCRIPTS / "ingestion_cli.py"
        result = p.run_script(cli, "recover", "fixture", DOCUMENT_ID)
        self.assertEqual(result.stdout, "idle\n")
        self.assertFalse(p.calls("curl"))
        self.fresh()
        item = p.payload()
        original = self.state()["last_success"]
        result = p.run_script(cli, "recover", "fixture", DOCUMENT_ID)
        self.assertEqual(result.stdout, "idle\n")
        payload = {"items": [item], "async": True}
        p.run_script(cli, "retain", "fixture", input=json.dumps(payload))
        receipt = self.state()["last_success"]
        self.assertEqual(len(p.posts()), 2)
        self.assertEqual(receipt["source_hash"], original["source_hash"])
        self.assertNotEqual(receipt["operation_id"], original["operation_id"])
        item["timestamp"] = "2026-09-05T01:02:03Z"
        p.run_script(cli, "retain", "fixture", input=json.dumps(payload))
        self.assertNotEqual(self.state()["last_success"]["source_hash"], receipt["source_hash"])

    def test_dry_run_never_recovers_or_uses_beads_or_network(self):
        p = self.pack
        p.env.pop("GC_CITY_PATH")
        p.env.pop("GC_SESSION_ID")
        p.env.pop("HINDSIGHT_WRITER")
        result = self.fresh("--dry-run")
        self.assertIn("WOULD RETAIN", result.stdout)
        self.assertFalse(p.calls())
        p.config["document"] = dict(content="Body", document_metadata=dict(title="Fixture", hit_count="2"),
                                    tags=["scope:repo", "repo:repo", "source:agent", "memory_type:gotcha", "status:draft"])
        result = self.memory("--dry-run", "--bump")
        payload = json.loads(result.stdout.split("\n", 1)[1])
        self.assertEqual(payload["items"][0]["metadata"]["hit_count"], "3")
        self.assertEqual([c["tool"] for c in p.calls()], ["hindsight"])

    def test_missing_city_or_beads_failure_prevents_bump_read_and_post(self):
        p = self.pack
        city = p.env.pop("GC_CITY_PATH")
        result = self.memory("--bump", code=2)
        self.assertIn("GC_CITY_PATH", result.stderr)
        self.assertFalse(p.calls("gc"))
        p.env["GC_CITY_PATH"] = city
        p.config["beads_error"] = True
        self.memory("--bump", code=5)
        self.assertFalse(p.posts())
        self.assertFalse(p.calls("hindsight"))

    def test_invalid_flags_type_and_status_remain_rejected(self):
        for args in (("--unknown",), ("--type", "invalid"), ("--status", "invalid")):
            with self.subTest(args=args):
                self.fresh("--dry-run", *args, code=2)
        self.assertFalse(self.pack.calls())

    def test_cli_rejects_bad_payload_without_external_calls_or_secrets(self):
        p = self.pack
        for payload in ("fixture-secret", "null", "[]", '{"items": [], "async": true}',
                        '{"items": [{}], "async": true}', '{"items": [{}], "async": false}'):
            with self.subTest(payload=payload):
                result = p.run_script(SCRIPTS / "ingestion_cli.py", "retain", "fixture", input=payload, code=2)
                self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        self.assertFalse(p.calls())


if __name__ == "__main__":
    unittest.main()
