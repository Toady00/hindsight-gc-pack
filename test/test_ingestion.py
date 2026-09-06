"""Offline ingestion recovery, Beads CLI, and curl contract tests."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import uuid


SPEC = importlib.util.spec_from_file_location(
    "ingestion", Path(__file__).resolve().parents[1] / "assets/scripts/ingestion.py"
)
ingestion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingestion)

WRITER = {"HINDSIGHT_WRITER": "archivist", "GC_SESSION_ID": "test-session", "GC_CITY_PATH": "/city"}
ITEM = {"document_id": "docs/a", "content": "Published body", "tags": ["scope:repo"],
        "metadata": {"content_hash": "hash-a", "repo": "project", "relpath": "docs/a.md",
                     "repository": "https://example.invalid/project", "source_commit": "commit-a"}}
SOURCE = {"repository": "https://example.invalid/project", "relpath": "docs/a.md"}


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.writes = []
        self.reject = lambda state: False

    def get(self, kind, document_id=""):
        return deepcopy(self.rows.get((kind, document_id)))

    def put(self, kind, document_id, data):
        if self.reject(data):
            raise ingestion.Error("store unavailable")
        self.writes.append(deepcopy(data))
        self.rows[kind, document_id] = deepcopy(data)
        return "city-record"


class MemoryAPI:
    def __init__(self):
        self.operations = {}
        self.posts = []
        self.capability_checks = 0
        self.initial_status = "completed"
        self.retry_status = "completed"
        self.reprocess_status = "completed"
        self.lose_ack = False
        self.lose_before_commit = False
        self.lose_reprocess_ack = False
        self.unavailable = False
        self.bad_ack = False
        self.extra_result = {}
        self.operation_results = {}
        self.gets = []
        self.checked = False

    def capabilities(self):
        self.capability_checks += 1
        self.checked = True

    def request(self, method, suffix, payload=None):
        assert method == "POST"
        self.posts.append((suffix, deepcopy(payload)))
        if suffix == "memories":
            assert self.checked
            self.checked = False
            op_id = payload["operation_id"]
            uuid.UUID(op_id)
            if self.lose_before_commit:
                self.lose_before_commit = False
                raise ingestion.Error("lost before commit")
            self.operations.setdefault(op_id, self.initial_status)
            if self.lose_ack:
                self.lose_ack = False
                raise ingestion.Error("lost acknowledgement")
            return {"operation_id": str(uuid.uuid4()) if self.bad_ack else op_id}
        if suffix.endswith("/retry"):
            op_id = suffix.split("/")[1]
            self.operations[op_id] = self.retry_status
            return {"success": True, "operation_id": op_id}
        if suffix.endswith("/reprocess"):
            op_id = str(uuid.uuid4())
            self.operations[op_id] = self.reprocess_status
            if self.lose_reprocess_ack:
                raise ingestion.Error("lost reprocess ack")
            return {"success": True, "operation_id": op_id}
        raise AssertionError(suffix)

    def operation(self, op_id):
        self.gets.append(op_id)
        if self.unavailable:
            raise ingestion.Error("operation lookup unavailable")
        details = self.operation_results.get(op_id, self.extra_result)
        if isinstance(details, Exception):
            raise details
        result = dict(operation_id=op_id, status=self.operations.get(op_id, "not_found"))
        result.update(details)
        return result


class IngestorTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, WRITER)
        env.start()
        self.addCleanup(env.stop)
        self.store, self.api = MemoryStore(), MemoryAPI()
        self.ingestor = ingestion.Ingestor(self.store, self.api)

    def retain(self, **kwargs):
        return self.ingestor.retain(deepcopy(ITEM), "hash-a", SOURCE, **kwargs)

    def state(self):
        return self.store.get("document", "docs/a")

    def child(self, status="completed", **details):
        op_id = str(uuid.uuid4())
        self.api.operation_results[op_id] = dict(status=status, **details)
        return {"operation_id": op_id, "status": "completed", "error_message": None}

    def assert_error(self, code, callable_, *args, **kwargs):
        with self.assertRaises(ingestion.Error) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def test_success_receipt_and_payload_lifetime(self):
        self.assertEqual(self.retain(), "shipped")
        state = self.state()
        self.assertEqual(state["attempt"]["state"], "succeeded")
        self.assertNotIn("payload", state["attempt"])
        self.assertEqual(state["last_success"]["source_hash"], "hash-a")
        self.assertEqual(state["source"], SOURCE)
        self.assertTrue(state["last_success"]["completed_at"].endswith("Z"))
        self.assertEqual(self.store.writes[0]["attempt"]["state"], "prepared")
        self.assertEqual(self.store.writes[0]["attempt"]["payload"]["items"], [ITEM])
        self.assertFalse(self.ingestor.recover("docs/a"))

    def test_never_skip_bank_hash_without_receipt(self):
        self.assertEqual(self.retain(bank_hash="hash-a"), "shipped")
        self.assertEqual(len(self.api.posts), 1)

    def test_skip_requires_both_hashes_and_bank_hash(self):
        self.retain()
        self.assertEqual(self.retain(bank_hash="hash-a"), "unchanged")
        self.assertEqual(self.retain(bank_hash="other"), "shipped")
        item = deepcopy(ITEM)
        item["tags"].append("domain:changed")
        self.assertEqual(self.ingestor.retain(item, "hash-a", SOURCE, bank_hash="hash-a"), "shipped")
        self.assertEqual(len(self.api.posts), 3)

    def test_commit_and_ref_are_bookkeeping_but_other_metadata_is_not(self):
        self.retain()
        item = deepcopy(ITEM)
        item["metadata"].update(source_commit="unrelated-commit", source_ref="main", ref="origin/main")
        self.assertEqual(self.ingestor.retain(item, "hash-a", SOURCE, bank_hash="hash-a"), "unchanged")
        item["metadata"]["repo"] = "different-repo"
        self.assertEqual(self.ingestor.retain(item, "hash-a", SOURCE, bank_hash="hash-a"), "shipped")

    def test_generated_timestamp_does_not_reship_unchanged_source(self):
        item = dict(deepcopy(ITEM), timestamp="2026-09-04T00:00:00Z")
        self.ingestor.retain(item, "hash-a", SOURCE)
        item["timestamp"] = "2026-09-05T00:00:00Z"
        self.assertEqual(self.ingestor.retain(item, "hash-a", SOURCE, bank_hash="hash-a"), "unchanged")
        self.assertEqual(self.ingestor.retain(item, "hash-b", SOURCE, bank_hash="hash-a"), "shipped")

    def test_partial_committed_hash_failure_retries_original_on_next_invocation(self):
        self.api.initial_status = "failed"
        self.assert_error(1, self.retain, bank_hash="hash-a")
        original = self.state()["attempt"]["operation_id"]
        self.assertNotIn("last_success", self.state())
        self.assertIn("payload", self.state()["attempt"])
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.retain(bank_hash="hash-a"), "shipped")
        self.assertEqual(self.api.posts[-1], ("operations/" + original + "/retry", None))
        self.assertEqual(self.state()["last_success"]["operation_id"], original)

    def test_failed_and_cancelled_recovery_retry_at_most_once(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.store, self.api = MemoryStore(), MemoryAPI()
                self.ingestor = ingestion.Ingestor(self.store, self.api)
                self.api.initial_status = status
                self.api.retry_status = status
                self.assert_error(1, self.retain)
                self.assert_error(1, self.ingestor.recover, "docs/a")
                self.assertEqual(len(self.api.posts), 2)
                self.assertEqual(self.state()["attempt"]["state"], "failed")

    def test_lost_ack_uses_operation_lookup_not_second_post(self):
        self.api.lose_ack = True
        self.assert_error(5, self.retain)
        self.assertEqual(self.state()["attempt"]["state"], "prepared")
        self.assertTrue(self.ingestor.recover("docs/a"))
        self.assertEqual(len(self.api.posts), 1)
        self.assertNotIn("payload", self.state()["attempt"])

    def test_not_found_replays_identical_payload_and_uuid_on_another_machine(self):
        self.api.lose_before_commit = True
        self.assert_error(5, self.retain)
        self.api.checked = False
        with patch.dict(os.environ, GC_CITY_PATH="/another/machine/city", GC_SESSION_ID="next-session"):
            restarted = ingestion.Ingestor(self.store, self.api)
            self.assertTrue(restarted.recover("docs/a"))
        self.assertEqual(self.api.posts[0], self.api.posts[1])
        self.assertEqual(len(self.api.operations), 1)

    def test_no_new_payload_can_replace_unavailable_unfinished_operation(self):
        self.api.lose_ack = True
        self.assert_error(5, self.retain)
        original = self.state()
        self.api.unavailable = True
        changed = dict(ITEM, content="Different body")
        self.assert_error(5, self.ingestor.retain, changed, "new-hash", SOURCE, bank_hash="new-hash")
        self.assertEqual(self.state(), original)
        self.assertEqual(len(self.api.posts), 1)

    def test_complete_receipt_store_failure_recovers_without_resubmit(self):
        self.store.reject = lambda state: state["attempt"]["state"] == "succeeded"
        self.assert_error(5, self.retain)
        self.assertEqual(self.state()["attempt"]["state"], "pending")
        self.assertIn("payload", self.state()["attempt"])
        self.store.reject = lambda state: False
        self.assertTrue(ingestion.Ingestor(self.store, self.api).recover("docs/a"))
        self.assertEqual(len(self.api.posts), 1)

    def test_store_failure_prevents_post(self):
        self.store.reject = lambda state: True
        self.assert_error(5, self.retain)
        self.assertEqual(self.api.posts, [])

    def test_save_uses_store_confirmation_without_another_read(self):
        with patch.object(self.store, "get") as read:
            self.ingestor._save("docs/a", {"document_id": "docs/a"})
        read.assert_not_called()
        self.assertEqual(self.store.writes, [{"document_id": "docs/a"}])

    def test_response_id_mismatch_keeps_original_intent(self):
        self.api.bad_ack = True
        self.assert_error(5, self.retain)
        self.assertEqual(self.state()["attempt"]["operation_id"], self.api.posts[0][1]["operation_id"])
        self.assertEqual(self.state()["attempt"]["state"], "prepared")

    def test_nested_extraction_errors_never_become_success(self):
        self.api.extra_result = {"result_metadata": {"batches": [{"result": {"extraction_errors_count": 2}}]}}
        self.assert_error(1, self.retain)
        self.assertNotIn("last_success", self.state())
        self.assert_error(1, self.ingestor.recover, "docs/a")
        self.assertEqual(len(self.api.posts), 1)
        self.assertIn("payload", self.state()["attempt"])

    def test_zero_extraction_errors_is_success(self):
        self.api.extra_result = {"result_metadata": {"extraction_errors_count": 0}}
        self.assertEqual(self.retain(), "shipped")

    def test_parent_completed_summary_does_not_hide_child_extraction_errors(self):
        child = self.child(result_metadata={"extraction_errors_count": 2})
        self.api.extra_result = {"operation_type": "batch_retain", "child_operations": [child]}
        self.assert_error(1, self.retain)
        self.assertIn(child["operation_id"], self.api.gets)
        self.assertEqual(self.state()["attempt"]["state"], "failed")
        self.assertNotIn("last_success", self.state())
        self.assertIn("payload", self.state()["attempt"])
        self.assert_error(1, self.ingestor.recover, "docs/a")
        self.assertEqual(len(self.api.posts), 1)

    def test_completed_descendants_are_fetched_recursively_without_requiring_private_metadata(self):
        grandchild = self.child(child_operations=None)
        child = self.child(child_operations=[grandchild])
        self.api.extra_result = {"operation_type": "batch_retain", "child_operations": [child]}
        self.assertEqual(self.retain(), "shipped")
        self.assertEqual(self.api.gets[1:], [child["operation_id"], grandchild["operation_id"]])
        self.assertEqual(self.state()["attempt"]["state"], "succeeded")

    def test_grandchild_extraction_failure_prevents_parent_receipt_and_force_post(self):
        grandchild = self.child(result_metadata={"nested": [{"extraction_errors_count": 1}]})
        child = self.child(child_operations=[grandchild])
        self.api.extra_result = {"child_operations": [child]}
        self.assert_error(1, self.retain, force=True)
        self.assertEqual([post[0] for post in self.api.posts], ["memories"])
        self.assertNotIn("last_success", self.state())

    def test_unavailable_or_pruned_child_never_counts_as_success(self):
        for result in (ingestion.Error("child lookup unavailable"), {"status": "not_found"}):
            with self.subTest(result=result):
                self.store, self.api = MemoryStore(), MemoryAPI()
                self.ingestor = ingestion.Ingestor(self.store, self.api)
                child = self.child()
                self.api.operation_results[child["operation_id"]] = result
                self.api.extra_result = {"child_operations": [child]}
                self.assert_error(5, self.retain)
                self.assertEqual(self.state()["attempt"]["state"], "unknown")
                self.assertNotIn("last_success", self.state())
                self.assertIn("payload", self.state()["attempt"])
                self.api.operation_results[child["operation_id"]] = {"status": "completed"}
                self.assertTrue(self.ingestor.recover("docs/a"))
                self.assertEqual(len(self.api.posts), 1)

    def test_pending_or_failed_child_detail_overrides_completed_parent_summary(self):
        for status, code, state in (("pending", 5, "unknown"), ("processing", 5, "unknown"),
                                    ("failed", 1, "failed"), ("cancelled", 1, "failed")):
            with self.subTest(status=status):
                self.store, self.api = MemoryStore(), MemoryAPI()
                self.ingestor = ingestion.Ingestor(self.store, self.api)
                child = self.child(status)
                self.api.extra_result = {"child_operations": [child]}
                self.assert_error(code, self.retain)
                self.assertEqual(self.state()["attempt"]["state"], state)
                self.assertNotIn("last_success", self.state())
                self.assert_error(code, self.ingestor.recover, "docs/a")
                self.assertEqual(len(self.api.posts), 1)

    def test_invalid_extraction_count_types_are_rejected_on_parent_and_child(self):
        for count in (False, True, 0.0, 1.0, "0", None, -1):
            for location in ("parent", "child"):
                with self.subTest(count=count, location=location):
                    self.store, self.api = MemoryStore(), MemoryAPI()
                    self.ingestor = ingestion.Ingestor(self.store, self.api)
                    details = {"result_metadata": {"extraction_errors_count": count}}
                    self.api.extra_result = details if location == "parent" else {"child_operations": [self.child(**details)]}
                    self.assert_error(5, self.retain)
                    self.assertEqual(self.state()["attempt"]["state"], "unknown")
                    self.assertNotIn("last_success", self.state())

    def test_child_validation_shares_deadline_and_stops_fetching_after_expiry(self):
        grandchild = self.child()
        child = self.child(child_operations=[grandchild])
        self.api.extra_result = {"child_operations": [child]}
        clock = [100.0]
        operation = self.api.operation

        def delayed_operation(op_id):
            self.assertEqual(self.api.deadline, 107)
            result = operation(op_id)
            if op_id == child["operation_id"]:
                clock[0] = 107
            return result

        with patch.object(ingestion.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.api, "operation", side_effect=delayed_operation):
            self.assert_error(3, self.retain, timeout=7)
        self.assertNotIn(grandchild["operation_id"], self.api.gets)
        self.assertEqual(self.state()["attempt"]["state"], "unknown")
        self.assertNotIn("last_success", self.state())

    def test_duplicate_and_cyclic_descendants_fail_without_repeated_fetch(self):
        for graph in ("duplicate", "self-cycle", "ancestor-cycle", "shared-descendant"):
            with self.subTest(graph=graph):
                self.store, self.api = MemoryStore(), MemoryAPI()
                self.ingestor = ingestion.Ingestor(self.store, self.api)
                self.api.lose_ack = True
                self.assert_error(5, self.retain)
                root_id = self.state()["attempt"]["operation_id"]
                root = {"operation_id": root_id, "status": "completed"}
                child = self.child()
                children = [child]
                if graph == "duplicate":
                    children.append(deepcopy(child))
                elif graph == "self-cycle":
                    children = [root]
                elif graph == "ancestor-cycle":
                    self.api.operation_results[child["operation_id"]]["child_operations"] = [root]
                else:
                    children.append(self.child(child_operations=[child]))
                self.api.extra_result = {"child_operations": children}
                self.assert_error(5, self.ingestor.recover, "docs/a")
                self.assertEqual(len(self.api.gets), len(set(self.api.gets)))
                self.assertEqual(self.state()["attempt"]["state"], "unknown")
                self.assertNotIn("last_success", self.state())
                self.assertEqual(len(self.api.posts), 1)

    def test_malformed_child_list_summary_or_detail_never_records_receipt(self):
        child_id = str(uuid.uuid4())
        for children in ({}, "bad", [None], [{}], [{"operation_id": child_id}],
                         [{"operation_id": child_id, "status": []}],
                         [{"operation_id": child_id, "status": "bogus"}],
                         [{"operation_id": "bad", "status": "completed"}]):
            with self.subTest(children=children):
                self.store, self.api = MemoryStore(), MemoryAPI()
                self.ingestor = ingestion.Ingestor(self.store, self.api)
                self.api.extra_result = {"child_operations": children}
                self.assert_error(5, self.retain)
                self.assertEqual(self.state()["attempt"]["state"], "unknown")
                self.assertNotIn("last_success", self.state())
        self.api.extra_result = {"child_operations": [self.child(operation_id=str(uuid.uuid4()))]}
        self.assert_error(5, self.ingestor.recover, "docs/a")
        self.assertNotIn("last_success", self.state())

    def test_empty_completed_batch_parent_is_not_proof_of_extraction(self):
        self.api.extra_result = {"operation_type": "batch_retain", "child_operations": []}
        self.assert_error(5, self.retain)
        self.assertEqual(self.state()["attempt"]["state"], "unknown")
        self.assertNotIn("last_success", self.state())

    def test_failed_batch_without_retryable_children_requires_manual_inspection(self):
        for children_status in (None, "completed", "pending", "processing"):
            with self.subTest(children_status=children_status):
                self.store, self.api = MemoryStore(), MemoryAPI()
                self.ingestor = ingestion.Ingestor(self.store, self.api)
                self.api.initial_status = "failed"
                children = [] if children_status is None else [dict(self.child(), status=children_status)]
                self.api.extra_result = {"operation_type": "batch_retain", "child_operations": children}
                self.assert_error(1, self.retain)
                message = self.assert_error(1, self.ingestor.recover, "docs/a")
                self.assertIn("manual inspection", message)
                self.assertEqual(self.state()["attempt"]["state"], "failed")
                self.assertIn("no retryable children", self.state()["attempt"]["error"])
                self.assertNotIn("last_success", self.state())
                self.assertEqual(len(self.api.posts), 1)

    def test_failed_batch_with_retryable_children_retries_only_once_per_recovery(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.store, self.api = MemoryStore(), MemoryAPI()
                self.ingestor = ingestion.Ingestor(self.store, self.api)
                self.api.initial_status = self.api.retry_status = "failed"
                child = dict(self.child(status), status=status)
                self.api.extra_result = {"operation_type": "batch_retain", "child_operations": [child]}
                self.assert_error(1, self.retain)
                self.assert_error(1, self.ingestor.recover, "docs/a")
                self.assertEqual(len(self.api.posts), 2)
                self.assertTrue(self.api.posts[-1][0].endswith("/retry"))
                self.assertEqual(self.state()["attempt"]["state"], "failed")

    def test_timeout_is_monotonic_bounded_and_does_not_destroy_attempt(self):
        self.api.initial_status = "processing"
        clock = [100.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with patch.object(ingestion.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(ingestion.time, "sleep", side_effect=sleep):
            self.assert_error(3, self.retain, timeout=7)
        self.assertEqual(sleeps, [5, 2])
        self.assertIn("payload", self.state()["attempt"])
        self.assertEqual(self.state()["attempt"]["state"], "pending")

    def test_force_waits_for_both_operations_before_receipt(self):
        self.assertEqual(self.retain(force=True), "shipped")
        self.assertEqual([x[0] for x in self.api.posts], ["memories", "documents/docs%2Fa/reprocess"])
        self.assertIn("reprocess_operation_id", self.state()["last_success"])
        prepared = next(x for x in self.store.writes if x["attempt"]["phase"] == "reprocess_prepared")
        self.assertNotIn("last_success", prepared)

    def test_force_lost_ack_fails_closed_on_restart(self):
        self.api.lose_reprocess_ack = True
        self.assert_error(5, self.retain, force=True)
        self.assertEqual(self.state()["attempt"]["phase"], "reprocess_prepared")
        self.assertNotIn("last_success", self.state())
        message = self.assert_error(5, self.retain, force=True, bank_hash="hash-a")
        self.assertIn("inspect Hindsight operation list", message)
        self.assertIn("repair bead", message)
        self.assertEqual(len(self.api.posts), 2)

    def test_force_ack_store_failure_has_same_ambiguity(self):
        self.store.reject = lambda state: state["attempt"]["phase"] == "reprocess_wait"
        self.assert_error(5, self.retain, force=True)
        self.assertEqual(self.state()["attempt"]["phase"], "reprocess_prepared")
        self.store.reject = lambda state: False
        self.assert_error(5, self.ingestor.recover, "docs/a")
        self.assertEqual(len(self.api.posts), 2)

    def test_force_failed_reprocess_resumes_without_extra_force_request(self):
        self.api.reprocess_status = "failed"
        self.assert_error(1, self.retain, force=True)
        reprocess_id = self.state()["attempt"]["reprocess_operation_id"]
        self.assertEqual(self.retain(force=True), "shipped")
        self.assertEqual(self.api.posts[-1][0], "operations/" + reprocess_id + "/retry")
        self.assertEqual(len(self.api.posts), 3)

    def test_force_after_recovering_ordinary_attempt_still_reprocesses(self):
        self.api.lose_ack = True
        self.assert_error(5, self.retain)
        original = self.state()["attempt"]["operation_id"]
        self.assertFalse(self.state()["attempt"]["reprocess"])
        self.assertEqual(self.retain(force=True, bank_hash="hash-a"), "shipped")
        self.assertEqual([post[0] for post in self.api.posts],
                         ["memories", "memories", "documents/docs%2Fa/reprocess"])
        self.assertNotEqual(self.state()["last_success"]["operation_id"], original)
        self.assertIn("reprocess_operation_id", self.state()["last_success"])

    def test_force_after_recovering_prior_force_does_not_retain_or_reprocess_twice(self):
        self.api.lose_ack = True
        self.assert_error(5, self.retain, force=True)
        original = self.state()["attempt"]["operation_id"]
        self.assertEqual(self.retain(force=True, bank_hash="hash-a"), "shipped")
        self.assertEqual([post[0] for post in self.api.posts], ["memories", "documents/docs%2Fa/reprocess"])
        self.assertEqual(self.state()["last_success"]["operation_id"], original)

    def test_recover_nonexistent_is_read_only_and_requires_no_writer(self):
        with patch.dict(os.environ, HINDSIGHT_WRITER="reader", GC_SESSION_ID=""):
            self.assertFalse(self.ingestor.recover("absent"))
            self.assert_error(2, self.retain)
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.store.writes, [])

    def test_manual_reprocess_repair_with_known_id_can_finish(self):
        self.api.lose_reprocess_ack = True
        self.assert_error(5, self.retain, force=True)
        state = self.state()
        known_id = next(key for key in self.api.operations if key != state["attempt"]["operation_id"])
        state["attempt"].update(phase="reprocess_wait", reprocess_operation_id=known_id)
        self.store.put("document", "docs/a", state)
        self.assertTrue(self.ingestor.recover("docs/a"))
        self.assertEqual(self.state()["last_success"]["reprocess_operation_id"], known_id)
        self.assertEqual(len(self.api.posts), 2)

    def test_reprocess_prepared_store_failure_prevents_reprocess_post(self):
        self.store.reject = lambda state: state["attempt"]["phase"] == "reprocess_prepared"
        self.assert_error(5, self.retain, force=True)
        self.assertEqual([post[0] for post in self.api.posts], ["memories"])
        self.assertEqual(self.state()["attempt"]["phase"], "retain")
        self.store.reject = lambda state: False
        self.assertTrue(self.ingestor.recover("docs/a"))
        self.assertEqual(len(self.api.posts), 2)

    def test_reprocess_not_found_is_not_replayed(self):
        self.api.reprocess_status = "failed"
        self.assert_error(1, self.retain, force=True)
        del self.api.operations[self.state()["attempt"]["reprocess_operation_id"]]
        self.assert_error(5, self.ingestor.recover, "docs/a")
        self.assertEqual(len(self.api.posts), 2)

    def test_recovery_finishes_old_revision_before_submitting_new_payload(self):
        self.api.lose_ack = True
        self.assert_error(5, self.retain)
        old_id = self.state()["attempt"]["operation_id"]
        item = dict(ITEM, content="new revision")
        self.assertEqual(self.ingestor.retain(item, "hash-b", SOURCE), "shipped")
        self.assertEqual(len(self.api.posts), 2)
        self.assertNotEqual(self.api.posts[1][1]["operation_id"], old_id)
        successes = [x for x in self.store.writes if x["attempt"]["state"] == "succeeded"]
        self.assertEqual([x["last_success"]["source_hash"] for x in successes], ["hash-a", "hash-b"])

    def test_malformed_attempt_never_replays_or_records_success(self):
        self.api.lose_ack = True
        self.assert_error(5, self.retain)
        original = self.state()
        for mutation in (
                lambda state: state.update(attempt={}),
                lambda state: state["attempt"].update(state="bogus"),
                lambda state: state["attempt"].update(phase="bogus"),
                lambda state: state["attempt"].pop("payload"),
                lambda state: state["attempt"]["payload"].update(items=["not an item"]),
                lambda state: state["attempt"]["payload"]["items"][0].update(content="tampered")):
            with self.subTest(mutation=mutation):
                state = deepcopy(original)
                mutation(state)
                self.store.rows["document", "docs/a"] = state
                self.assert_error(5, self.ingestor.recover, "docs/a")
                self.assertNotIn("last_success", self.state())
        self.assertEqual(len(self.api.posts), 1)

    def test_capability_failure_prevents_any_post_or_prepared_write(self):
        with patch.object(self.api, "capabilities", side_effect=ingestion.Error("unsupported", 2)):
            self.assert_error(2, self.retain)
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.store.writes, [])

    def test_replay_checks_capabilities_and_retains_intent_on_failure(self):
        self.api.lose_before_commit = True
        self.assert_error(5, self.retain)
        original = self.state()
        with patch.object(self.api, "capabilities", side_effect=ingestion.Error("unsupported", 2)):
            self.assert_error(2, self.ingestor.recover, "docs/a")
        self.assertEqual(self.state(), original)
        self.assertEqual(len(self.api.posts), 1)


class BeadsCLITest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, dict(WRITER, GC_BIN="custom-gc", HINDSIGHT_API_KEY="TOP-SECRET"))
        env.start()
        self.addCleanup(env.stop)
        self.rows = []
        self.calls = []
        self.custom_types = "existing,other"
        self.fail_read = False
        self.create_error = False
        self.drop_write = False
        mock = patch.object(ingestion.subprocess, "run", side_effect=self.run_cli)
        mock.start()
        self.addCleanup(mock.stop)
        self.store = ingestion.BeadsStore("https://memory.invalid", "bank")

    def run_cli(self, command, **kwargs):
        self.assertEqual(command[:4], ["custom-gc", "bd", "--city", self.store.city])
        self.assertNotIn("TOP-SECRET", str(command))
        args = command[4:]
        self.calls.append(args)
        result = {}
        code = 0
        if args[0] == "list":
            self.assertEqual(args[:5], ["list", "--all", "--limit", "0", "--label"])
            self.assertEqual(args[-1], "--json")
            result = [row for row in self.rows if args[5] in row["labels"]]
            code = 1 if self.fail_read else 0
        elif args[:3] == ["config", "get", "types.custom"]:
            result = {"key": "types.custom", "value": self.custom_types}
        elif args[:3] == ["config", "set", "types.custom"]:
            self.custom_types = args[3]
        elif args[0] in ("create", "update"):
            metadata_arg = args[args.index("--metadata") + 1]
            metadata = json.loads(metadata_arg)
            self.assertEqual(set(metadata), {"hindsight"})
            if args[0] == "create":
                self.assertNotIn("--id", args)
                self.assertNotIn("--ephemeral", args)
                self.assertNotIn("--no-history", args)
                row = {"id": "arbitrary-prefix-1", "status": args[args.index("--status") + 1],
                       "issue_type": args[args.index("--type") + 1], "metadata": metadata,
                       "labels": args[args.index("--label") + 1].split(",")}
                if not self.drop_write:
                    self.rows.append(row)
                result = row
                code = 1 if self.create_error else 0
            else:
                row = next(row for row in self.rows if row["id"] == args[1])
                if not self.drop_write:
                    row["metadata"].update(metadata)
                result = [row]
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(command, code, json.dumps(result), "TOP-SECRET failure")

    def put(self):
        return self.store.put("document", "docs/a", {"source": SOURCE, "attempt": {"state": "prepared"}})

    def test_read_only_never_registers_types_or_initializes(self):
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.get("document", "docs/a"))
        self.assertEqual(self.store.list_documents(), [])
        self.assertTrue(all(call[0] == "list" for call in self.calls))

    def test_persistent_record_create_and_atomic_update_preserve_metadata(self):
        bead_id = self.put()
        self.assertEqual(bead_id, "arbitrary-prefix-1")
        self.assertEqual(self.rows[0]["status"], "pinned")
        self.assertEqual(self.rows[0]["issue_type"], "hindsight-document")
        self.rows[0]["metadata"]["unrelated"] = {"keep": True}
        self.store.put("document", "docs/a", {"source": SOURCE, "attempt": {"state": "succeeded"}})
        self.assertEqual(self.rows[0]["metadata"]["unrelated"], {"keep": True})
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.custom_types, "existing,other,hindsight-document,hindsight-bank")
        self.assertEqual(sum(call[:2] == ["config", "get"] for call in self.calls), 1)
        self.assertEqual(self.calls[-1][0], "list")

    def test_existing_types_need_no_config_set(self):
        self.custom_types = "hindsight-document,other,hindsight-bank"
        self.put()
        self.assertFalse(any(call[:2] == ["config", "set"] for call in self.calls))

    def test_stable_json_tuple_key_ignores_machine_city_path(self):
        self.put()
        key = self.rows[0]["labels"][1]
        self.assertEqual(key, "hindsight:key:" + ingestion._hash(
            ["https://memory.invalid", "bank", "document", "docs/a"]))
        with patch.dict(os.environ, GC_CITY_PATH="/another/city"):
            self.store = ingestion.BeadsStore("https://memory.invalid/", "bank")
            self.assertEqual(self.store.get("document", "docs/a")["document_id"], "docs/a")
        self.assertEqual(len(self.rows), 1)

    def test_bank_record_and_document_inventory(self):
        self.store.put("bank", "", {"scan": "healthy"})
        self.assertEqual(self.rows[0]["issue_type"], "hindsight-bank")
        self.put()
        self.assertEqual([x["document_id"] for x in self.store.list_documents()], ["docs/a"])

    def test_read_failure_never_creates_and_never_leaks_tool_errors(self):
        self.fail_read = True
        with self.assertRaises(ingestion.Error) as caught:
            self.put()
        self.assertEqual(caught.exception.code, 5)
        self.assertNotIn("TOP-SECRET", str(caught.exception))
        self.assertEqual([call[0] for call in self.calls], ["list"])

    def test_create_commits_then_errors_does_not_duplicate_on_next_put(self):
        self.create_error = True
        with self.assertRaises(ingestion.Error):
            self.put()
        self.assertEqual(len(self.rows), 1)
        self.create_error = False
        self.put()
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(sum(call[0] == "create" for call in self.calls), 1)

    def test_unconfirmed_create_fails_readback(self):
        self.drop_write = True
        with self.assertRaisesRegex(ingestion.Error, "read-back"):
            self.put()

    def test_duplicate_matching_key_fails_get_list_and_put(self):
        self.put()
        self.rows.append(deepcopy(self.rows[0]))
        self.rows[1]["id"] = "duplicate"
        for method in (lambda: self.store.get("document", "docs/a"), self.store.list_documents, self.put):
            with self.assertRaisesRegex(ingestion.Error, "duplicate"):
                method()

    def test_get_rejects_wrong_identity_schema_and_lifecycle(self):
        self.put()
        original = deepcopy(self.rows[0])
        mutations = [
            lambda row: row.update(status="open"),
            lambda row: row.update(assignee="archivist"),
            lambda row: row["metadata"].update({"gc.routed_to": "archivist"}),
            lambda row: row.update(ephemeral=True),
            lambda row: row.update(no_history=True),
            lambda row: row.update(is_template=True),
            lambda row: row.update(wisp_plane=True),
            lambda row: row["labels"].append("gc:agent"),
            lambda row: row["labels"].append("gc.routed_to:archivist"),
            lambda row: row["metadata"]["hindsight"].update(schema_version=2),
            lambda row: row["metadata"]["hindsight"].update(schema_version=True),
            lambda row: row["metadata"]["hindsight"].update(bank="another-bank"),
            lambda row: row["metadata"]["hindsight"].update(document_id="another-doc"),
            lambda row: row["metadata"]["hindsight"]["data"].update(document_id="another-doc"),
            lambda row: row.update(issue_type="task"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.rows = [deepcopy(original)]
                mutate(self.rows[0])
                with self.assertRaises(ingestion.Error):
                    self.store.get("document", "docs/a")

    def test_requires_absolute_city_without_subprocess(self):
        for city in ("", "relative/path"):
            with patch.dict(os.environ, GC_CITY_PATH=city), self.assertRaises(ingestion.Error) as caught:
                ingestion.BeadsStore("https://memory.invalid", "bank")
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.calls, [])

    def test_list_requires_bare_array_not_wrapped_or_null_output(self):
        for response in ({"issues": []}, None, ["not a record"]):
            with patch.object(self.store, "_call", return_value=response), self.assertRaises(ingestion.Error):
                self.store.get("document", "docs/a")

    def test_malformed_config_value_prevents_create(self):
        self.custom_types = None
        with self.assertRaisesRegex(ingestion.Error, "types.custom"):
            self.put()
        self.assertFalse(any(call[0] == "create" for call in self.calls))

    def test_bead_id_not_persisted(self):
        self.store.put("document", "docs/a", {"bead_id": "not-a-field", "source": SOURCE})
        self.assertNotIn("bead_id", self.store.get("document", "docs/a"))


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.api = ingestion.API("https://memory.invalid/api/", "bank /name")

    def test_curl_flags_auth_stdin_body_file_and_global_route(self):
        paths = []

        def run(command, **kwargs):
            self.assertNotIn("VERY-SECRET", str(command))
            self.assertNotIn("large body", str(command))
            self.assertEqual(command[:8], ["curl", "--config", "-", "--silent", "--show-error",
                                          "--fail-with-body", "--connect-timeout", "15"])
            self.assertIn('--max-time', command)
            self.assertIn('header = "Authorization: Bearer VERY-SECRET"', kwargs["input"])
            if "--data-binary" in command:
                self.assertIn("https://memory.invalid/api/v1/default/banks/bank%20%2Fname/memories", command)
                path = Path(command[command.index("--data-binary") + 1][1:])
                self.assertEqual(json.loads(path.read_text()), {"content": "large body"})
                paths.append(path)
            else:
                self.assertIn("https://memory.invalid/api/openapi.json", command)
            return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

        with patch.dict(os.environ, HINDSIGHT_API_KEY="VERY-SECRET"), \
                patch.object(ingestion.subprocess, "run", side_effect=run):
            self.assertEqual(self.api.request("POST", "memories", {"content": "large body"}), {"ok": True})
            self.api.request("GET", "openapi.json", global_=True)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_network_error_output_and_exception_do_not_expose_credentials(self):
        for result in (subprocess.CompletedProcess([], 22, "VERY-SECRET", "VERY-SECRET"),
                       subprocess.TimeoutExpired(["curl", "VERY-SECRET"], 120), OSError("VERY-SECRET")):
            with self.subTest(result=result):
                with patch.object(ingestion.subprocess, "run") as run:
                    if isinstance(result, Exception):
                        run.side_effect = result
                    else:
                        run.return_value = result
                    with self.assertRaises(ingestion.Error) as caught:
                        self.api.request("GET", "documents")
                self.assertNotIn("VERY-SECRET", str(caught.exception))
                self.assertEqual(caught.exception.code, 5)

    def test_invalid_http_json_or_non_object_fails(self):
        for body in ("not json", "[]", "null"):
            with patch.object(ingestion.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, body, "")), \
                    self.assertRaises(ingestion.Error):
                self.api.request("GET", "documents")

    def test_credentials_in_url_are_rejected(self):
        for url in ("https://user:secret@memory.invalid", "https://memory.invalid?key=secret"):
            with self.assertRaises(ingestion.Error) as caught:
                ingestion.API(url, "bank")
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("secret", str(caught.exception))

    def test_capabilities_fail_closed_and_cache_only_success(self):
        valid = {"components": {"schemas": {"RetainRequest": {"properties": {
            "operation_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "async": {"type": "boolean"}}}}}}
        with patch.object(self.api, "request", return_value={}) as request:
            with self.assertRaises(ingestion.Error) as caught:
                self.api.capabilities()
            self.assertEqual(caught.exception.code, 2)
            request.return_value = valid
            self.api.capabilities()
            self.api.capabilities()
            self.assertEqual(request.call_count, 2)
            request.assert_called_with("GET", "openapi.json", global_=True)

    def test_operation_validates_exact_id_and_status(self):
        for status in ingestion.API.STATUSES:
            with patch.object(self.api, "request", return_value={"operation_id": "op", "status": status}):
                self.assertEqual(self.api.operation("op")["status"], status)
        for value in ({"status": "completed"}, {"operation_id": "op", "status": "unknown"},
                      {"operation_id": "wrong", "status": "completed"}, {"operation_id": "op", "status": []}):
            with patch.object(self.api, "request", return_value=value), self.assertRaises(ingestion.Error):
                self.api.operation("op")

    def test_drain_uses_filtered_totals_not_recent_page(self):
        pages = [{"total": 500, "operations": [{"status": "pending"}]},
                 {"total": 0, "operations": []}, {"total": 0, "operations": []},
                 {"total": 0, "operations": []}]
        with patch.object(self.api, "request", side_effect=pages) as request, \
                patch.object(ingestion.time, "sleep") as sleep:
            self.api.drain(30)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual([call.args[1] for call in request.call_args_list],
                         ["operations?status=pending&limit=1", "operations?status=processing&limit=1"] * 2)

    def test_drain_rejects_invalid_totals_and_statuses(self):
        for value in ({"total": True, "operations": []}, {"total": -1, "operations": []},
                      {"total": 1.0, "operations": []}, {"total": 1, "operations": []},
                      {"total": 1, "operations": [{"status": "completed"}]},
                      {"total": 0, "operations": {}}, {}):
            with patch.object(self.api, "request", return_value=value), self.assertRaises(ingestion.Error):
                self.api.drain(30)

    def test_inventory_pages_and_ids(self):
        with patch.object(self.api, "request", side_effect=[
                {"items": [{"id": "a"}], "total": 2}, {"items": [{"id": "b"}], "total": 2}]) as request:
            self.assertEqual(self.api.inventory(), [{"id": "a"}, {"id": "b"}])
        self.assertEqual(request.call_args_list[1].args[1], "documents?limit=100&offset=1")

    def test_inventory_rejects_truncation_duplicates_and_invalid_shapes(self):
        cases = [[{"items": [], "total": 1}], [{"items": [], "total": True}],
                 [{"items": {}, "total": 0}], [{"items": [{}], "total": 1}],
                 [{"items": [{"id": "a"}, {"id": "a"}], "total": 2}],
                 [{"items": [{"id": "a"}], "total": 2}, {"items": [], "total": 2}],
                 [{"items": [{"id": "a"}], "total": 2}, {"items": [{"id": "b"}], "total": 3}]]
        for pages in cases:
            with self.subTest(pages=pages), patch.object(self.api, "request", side_effect=pages), \
                    self.assertRaises(ingestion.Error):
                self.api.inventory()

    def test_timeout_validation_and_curl_remaining_budget(self):
        for timeout in (-1, True, float("nan"), float("inf"), "3"):
            with self.assertRaises(ingestion.Error) as caught:
                self.api.drain(timeout)
            self.assertEqual(caught.exception.code, 2)
        with patch.object(ingestion.time, "monotonic", return_value=100), \
                patch.object(ingestion.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as run:
            self.api.deadline = 103
            self.api.request("GET", "documents")
            command = run.call_args.args[0]
            self.assertEqual(float(command[command.index("--max-time") + 1]), 3)
            self.api.deadline = 100
            with self.assertRaises(ingestion.Error) as caught:
                self.api.request("GET", "documents")
            self.assertEqual(caught.exception.code, 3)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
