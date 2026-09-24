"""Receipt-backed task reports and shipping exclusion across process resets."""
from copy import deepcopy
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets/scripts"))
import ingestion
from ship_report import ScanReport, status


class Store:
    api, bank = "https://fixture.invalid", "fixture"

    def __init__(self, city):
        self.city, self.rows = city, {}

    def get(self, kind, document_id=""):
        return deepcopy(self.rows.get((kind, document_id)))

    def put(self, kind, document_id, data):
        self.rows[kind, document_id] = deepcopy(data)
        return "fixture-record"

    def list_documents(self):
        return [deepcopy(value) for (kind, _), value in self.rows.items() if kind == "document"]


class FakeAPI:
    def __init__(self):
        self.operations, self.posts = {}, []

    def capabilities(self):
        pass

    def request(self, method, suffix, payload=None):
        import uuid
        self.posts.append(suffix)
        op_id = payload["operation_id"] if payload else str(uuid.uuid4())
        self.operations[op_id] = dict(operation_id=op_id, status="completed")
        return dict(operation_id=op_id, success=True)

    def operation(self, op_id):
        return self.operations[op_id]


class ShippingContinuityTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store, self.api = Store(temp.name), FakeAPI()
        env = patch.dict(os.environ, HINDSIGHT_WRITER="archivist", GC_SESSION_ID="fixture-session")
        env.start()
        self.addCleanup(env.stop)
        self.item = dict(document_id="doc-a", content="English body", tags=["scope:repo"])
        self.source = dict(kind="git", repository="repo", relpath="docs/a.md")

    def test_reset_then_zero_write_timeout_keeps_task_successes_visible(self):
        first = ScanReport(self.store, True, work_id="task-a")
        first.run["roots"] = [dict(status="ok")]
        first.start()
        ingestion.Ingestor(self.store, self.api, work_id="task-a").retain(self.item, "hash", self.source)
        # Simulate loss before scan.finish; only the document receipt survives.
        second = ScanReport(self.store, True, work_id="task-a")
        second.start()
        second.run.update(error="bank drain timeout", counts=dict(shipped=0))
        second.finish(3)
        report = status(self.store, "task-a")
        self.assertFalse(report["healthy"])
        self.assertEqual(report["unresolved_documents"], [])
        self.assertEqual(report["latest_run"]["counts"]["shipped"], 0)
        self.assertEqual([r["document_id"] for r in report["document_receipts"]], ["doc-a"])
        self.assertEqual([r["status"] for r in report["recent_scans"]], ["running", "incomplete"])
        self.assertEqual(status(self.store, "unrelated-task")["document_receipts"], [])
        self.assertEqual(ingestion.Ingestor(self.store, self.api, work_id="task-a").retain(
            self.item, "hash", self.source, bank_hash="hash"), "unchanged")
        self.assertEqual(self.api.posts, ["memories"])

    def test_reset_does_not_repeat_confirmed_force_request_for_same_task(self):
        ingestion.Ingestor(self.store, self.api, work_id="task-a").retain(
            self.item, "hash", self.source, force=True)
        restarted = ingestion.Ingestor(self.store, self.api, work_id="task-a")
        self.assertEqual(restarted.retain(self.item, "hash", self.source, bank_hash="hash", force=True), "unchanged")
        self.assertEqual(self.api.posts, ["memories", "documents/doc-a/reprocess"])
        ingestion.Ingestor(self.store, self.api, work_id="task-b").retain(
            self.item, "hash", self.source, bank_hash="hash", force=True)
        self.assertEqual(len(self.api.posts), 4, "a separately requested reprocess must still run")

    def test_recovery_keeps_original_task_attribution(self):
        first = ingestion.Ingestor(self.store, self.api, work_id="task-a")
        with patch.object(first, "_finish", side_effect=ingestion.Error("interrupted", 3)), self.assertRaises(ingestion.Error):
            first.retain(self.item, "hash", self.source)
        ingestion.Ingestor(self.store, self.api, work_id="task-b").recover("doc-a")
        self.assertEqual(status(self.store, "task-a")["document_receipts"][0]["document_id"], "doc-a")
        self.assertEqual(status(self.store, "task-b")["document_receipts"], [])
        self.assertEqual(self.api.posts, ["memories"])

    def test_legacy_receipts_are_not_retroactively_assigned(self):
        ingestion.Ingestor(self.store, self.api).retain(self.item, "hash", self.source)
        self.assertEqual(len(status(self.store)["document_receipts"]), 1)
        self.assertEqual(status(self.store, "task-a")["document_receipts"], [])

    def test_live_ship_lock_refuses_overlap_and_releases_after_exit(self):
        with ingestion.ship_lock(self.store):
            with self.assertRaisesRegex(ingestion.Error, "another ship process"):
                with ingestion.ship_lock(self.store):
                    self.fail("second ship entered")
        with ingestion.ship_lock(self.store):
            pass

    def test_scan_history_is_bounded_and_does_not_manufacture_success(self):
        for index in range(24):
            scan = ScanReport(self.store, True, work_id="task-a")
            scan.run["roots"] = [dict(status="ok")]
            scan.start()
            scan.finish(3)
        report = status(self.store, "task-a")
        self.assertEqual(len(report["recent_scans"]), 21)
        self.assertEqual(report["document_receipts"], [])
        self.assertFalse(report["healthy"])
        self.assertIsNone(report["last_success_at"])

    def test_current_work_uses_canonical_claim_not_lagging_controller_anchor(self):
        with patch.dict(os.environ, GC_CITY_PATH=self.store.city):
            store = ingestion.BeadsStore(self.store.api, self.store.bank)
        session = dict(id="fixture-session", metadata=dict(current_claim_bead_id="new-task",
                       currently_processing_bead_id="old-task", alias="hindsight.archivist"))
        with patch.object(store, "_call", side_effect=[[session], [dict(id="new-task", status="in_progress",
                          assignee="hindsight.archivist")]]):
            self.assertEqual(store.current_work(), "new-task")
        with patch.object(store, "_call", side_effect=[[session], [dict(id="new-task", status="closed",
                          assignee="hindsight.archivist")]]), self.assertRaises(ingestion.Error):
            store.current_work()

    def test_current_work_rejects_malformed_reads_and_never_uses_old_anchor(self):
        with patch.dict(os.environ, GC_CITY_PATH=self.store.city):
            store = ingestion.BeadsStore(self.store.api, self.store.bank)
        for result in ([], ["bad"], {}, [dict(id="wrong")]):
            with self.subTest(result=result), patch.object(store, "_call", return_value=result), \
                    self.assertRaises(ingestion.Error):
                store.current_work()
        with patch.object(store, "_call", return_value=[dict(id="fixture-session",
                          metadata=dict(currently_processing_bead_id="old-task"))]):
            self.assertEqual(store.current_work(), "")


if __name__ == "__main__":
    unittest.main()
