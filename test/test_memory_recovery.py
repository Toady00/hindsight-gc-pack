"""Memory CLI contracts and shell recovery-before-bump ordering.

Operation replay, lost acknowledgements, and receipt failures are exhaustively
covered in test_ingestion; only the memory-specific composition is exercised here.
"""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

try:
    from .pack_fixture import SCRIPTS, ShellFixture
except ImportError:
    from pack_fixture import SCRIPTS, ShellFixture

sys.path.insert(0, str(SCRIPTS))
import ingestion_cli
from ingestion import Error

DOCUMENT_ID = "gotcha.fixture"


class MemoryCLITest(unittest.TestCase):
    def setUp(self):
        self.store, self.ingestor = Mock(), Mock()
        self.store.get.return_value = {"last_success": {"operation_id": "fixture-operation"}}
        for name, replacement in (("BeadsStore", Mock(return_value=self.store)),
                                  ("API", Mock()), ("Ingestor", Mock(return_value=self.ingestor))):
            p = patch.object(ingestion_cli, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"HINDSIGHT_WRITER": "archivist", "GC_SESSION_ID": "fixture",
                                     "HINDSIGHT_API_URL": "https://fixture.invalid"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *args, input="", code=0):
        with patch.object(sys, "stdin", io.StringIO(input)), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ingestion_cli.main(list(args)), code)
        return out.getvalue(), err.getvalue()

    def test_retain_hashes_every_field_and_does_not_suppress_deliberate_reports(self):
        item = dict(document_id=DOCUMENT_ID, content="Body", timestamp="2026-09-05T00:00:00Z", metadata={"hit_count": "1"})
        hashes = []
        for timestamp in (item["timestamp"], item["timestamp"], "2026-09-05T01:00:00Z"):
            item["timestamp"] = timestamp
            output, _ = self.cli("retain", "fixture", input=json.dumps({"items": [item], "async": True}))
            self.assertEqual(output, f"RETAINED {DOCUMENT_ID} op=fixture-operation\n")
            call = self.ingestor.retain.call_args
            self.assertEqual(call.args, (item,))
            expected = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
            self.assertEqual(call.kwargs, dict(source_hash=expected, source={"kind": "agent-memory"}, bank_hash=None))
            hashes.append(expected)
        self.assertEqual(self.ingestor.retain.call_count, 3)
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[1], hashes[2])

    def test_recovery_markers_and_errors_propagate_without_retain(self):
        for recovered, expected in ((False, "idle\n"), (True, "recovered\n")):
            self.ingestor.recover.return_value = recovered
            self.assertEqual(self.cli("recover", "fixture", DOCUMENT_ID)[0], expected)
            self.ingestor.recover.assert_called_with(DOCUMENT_ID)
        self.ingestor.recover.side_effect = Error("operation unavailable")
        out, err = self.cli("recover", "fixture", DOCUMENT_ID, code=5)
        self.assertEqual(out, "")
        self.assertIn("operation unavailable", err)
        self.ingestor.retain.assert_not_called()

    def test_bad_payload_envelopes_do_not_reach_ingestion_or_leak_input(self):
        for payload in ("fixture-secret", "null", "[]", '{"items": [], "async": true}',
                        '{"items": [null], "async": true}', '{"items": [{}], "async": false}'):
            with self.subTest(payload=payload):
                out, err = self.cli("retain", "fixture", input=payload, code=2)
                self.assertNotIn("fixture-secret", out + err)
        self.ingestor.retain.assert_not_called()
        self.store.get.assert_not_called()

    def test_writer_and_connection_failures_prevent_service_construction(self):
        os.environ["HINDSIGHT_WRITER"] = "worker"
        self.cli("recover", "fixture", DOCUMENT_ID, code=2)
        os.environ["HINDSIGHT_WRITER"] = "archivist"
        os.environ["HINDSIGHT_API_URL"] = "https://user:fixture-secret@fixture.invalid"
        out, err = self.cli("recover", "fixture", DOCUMENT_ID, code=2)
        self.assertNotIn("fixture-secret", out + err)
        ingestion_cli.BeadsStore.assert_not_called()
        ingestion_cli.API.assert_not_called()


class MemoryShellTest(ShellFixture):
    def memory(self, *args, **kwargs):
        return self.run_script(SCRIPTS / "memory-retain.sh", "--id", DOCUMENT_ID, *args, **kwargs)

    def test_completion_write_failure_recovers_before_bump_or_content_read(self):
        self.config["completion_write_error"] = True
        self.memory("--title", "Fixture", "--repos", "repo", input="Symptom, cause, fix.", code=5)
        state = self.state(DOCUMENT_ID)
        op = state["attempt"]["operation_id"]
        self.assertEqual(state["attempt"]["state"], "pending")
        self.assertNotIn("last_success", state)
        item = self.posts()[0]["payload"]["items"][0]
        self.assertEqual(item["metadata"]["hit_count"], "1")
        self.assertNotIn("repo", item["metadata"])
        self.assertIn("status:accepted", item["tags"])
        self.assertEqual(item["strategy"], "gotcha")
        # A still-unavailable receipt store must stop before reading the bump.
        result = self.memory("--bump", "--content-file", self.root / "must-not-read", code=5)
        self.assertNotIn("RECOVERED", result.stdout)
        self.assertEqual(self.state(DOCUMENT_ID), state)
        self.assertFalse(self.calls("hindsight"))
        self.assertEqual(len(self.posts()), 1)
        self.config.clear()
        before = len(self.calls())
        result = self.memory("--bump", "--content-file", self.root / "must-not-read")
        self.assertIn(f"RECOVERED {DOCUMENT_ID}", result.stdout)
        self.assertIn("no additional hit counted; run again for a separate report", result.stdout)
        self.assertEqual(len(self.posts()), 1)
        self.assertFalse(self.calls("hindsight"))
        self.assertEqual(self.state(DOCUMENT_ID)["last_success"]["operation_id"], op)
        calls = self.calls()[before:]
        self.assertEqual([c["tool"] for c in calls[:3]], ["curl", "curl", "gc"])
        self.assertTrue(all(any("/operations?" in a for a in c["args"]) for c in calls[:2]))
        self.config["document"] = json.loads((self.root / "server.json").read_text())["documents"][0]
        result = self.memory("--bump")
        self.assertIn("hit_count=2", result.stdout)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.posts()[-1]["payload"]["items"][0]["metadata"]["hit_count"], "2")
        self.assertNotEqual(self.state(DOCUMENT_ID)["last_success"]["operation_id"], op)

    def test_dry_run_has_no_recovery_and_bump_preserves_status(self):
        for name in ("GC_CITY_PATH", "GC_SESSION_ID", "HINDSIGHT_WRITER"):
            self.env.pop(name)
        result = self.memory("--dry-run", "--title", "Fixture", "--repos", "repo", input="Body")
        self.assertIn("WOULD RETAIN", result.stdout)
        self.assertFalse(self.calls())
        self.config["document"] = dict(content="Body", document_metadata=dict(title="Fixture", hit_count="2"),
                                       tags=["scope:repo", "repo:repo", "source:agent", "memory_type:gotcha", "status:deprecated"])
        result = self.memory("--dry-run", "--bump")
        item = json.loads(result.stdout.split("\n", 1)[1])["items"][0]
        self.assertEqual(item["metadata"]["hit_count"], "3")
        self.assertIn("status:deprecated", item["tags"])
        self.assertEqual([c["tool"] for c in self.calls()], ["hindsight"])
        self.config["document"]["tags"].remove("status:deprecated")
        self.memory("--dry-run", "--bump", code=2)
        self.assertFalse(self.posts())

    def test_invalid_shell_flags_type_and_status_are_rejected(self):
        for args in (("--unknown",), ("--type", "invalid"), ("--status", "invalid")):
            with self.subTest(args=args):
                self.memory("--dry-run", "--title", "Fixture", "--repos", "repo", *args, input="Body", code=2)
        self.assertFalse(self.calls())


if __name__ == "__main__":
    unittest.main()
