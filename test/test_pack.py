"""Schema and ship orchestration tests; Git and ingestion permutations live below us."""
import base64
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

try:
    from .pack_fixture import DOC, PACK, SCRIPTS, ShellFixture
except ImportError:
    from pack_fixture import DOC, PACK, SCRIPTS, ShellFixture

sys.path.insert(0, str(SCRIPTS))
import ship_docs
import ship_report
import connection
from ingestion import Error, _payload_hash


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schema = load_module("docs_schema", PACK / "schemas/docs/validate.py")
request = load_module("ship_request", SCRIPTS / "ship-request.py")
FIELDS = dict(schema_version=2, id="spec.fixture", type="spec", title="Fixture", status="draft",
              source="agent", scope="repo", repos=["repo"], updated_at="2026-09-04T00:00:00Z")
DOCUMENT = dict(content=DOC, repo="repo", repository="https://fixture.invalid/repo",
                relpath="docs/spec.md", ref="refs/heads/main", commit="abc123")
ROOT = dict(root="/repo/docs", status="ok", detail="", repo="repo", repository=DOCUMENT["repository"],
            prefix="docs", ref=DOCUMENT["ref"], commit=DOCUMENT["commit"])


def report_store():
    """Mock the persistence boundary, retaining value-copy semantics."""
    rows = {}
    store = Mock(city="/city")
    store.get.side_effect = lambda kind, document_id="": deepcopy(rows.get((kind, document_id)))

    def put(kind, document_id, data):
        rows[kind, document_id] = deepcopy(data)
        return "fixture-record"

    store.put.side_effect = put
    store.list_documents.return_value = []
    return store


class SchemaTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_every_type_requires_status_and_matches_audit_vocabulary(self):
        types = json.loads((PACK / "schemas/docs/audit-vocab.json").read_text())["closed"]["memory_type"]
        self.assertEqual(set(types), set(schema.STRATEGIES))
        for kind in types:
            with self.subTest(kind=kind):
                fields = dict(FIELDS, type=kind)
                self.assertEqual(schema.validate(fields)["strategy"], schema.STRATEGIES[kind])
                del fields["status"]
                with self.assertRaisesRegex(ValueError, "status"):
                    schema.validate(fields)

    def test_status_is_context_not_strategy_or_observation_scope(self):
        for status in ("draft", "accepted", "superseded", "deprecated"):
            with self.subTest(status=status):
                verdict = schema.validate(dict(FIELDS, status=status))
                self.assertEqual(verdict["strategy"], "design-record")
                self.assertIn(f"status:{status}", verdict["tags"])
                self.assertIn(status.upper(), verdict["context"])
                self.assertFalse(any("status:" in tag for scope in verdict["observation_scopes"] for tag in scope))

    def test_bad_fields_and_unclaimed_frontmatter(self):
        for changes in ({"id": None}, {"updated_at": "nonsense"}, {"updated_at": "2026-02-30T00:00:00Z"},
                        {"repos": "repo"}, {"repos": [None]}, {"repos": []}, {"source": True},
                        {"title": ""}, {"schema_version": 3}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                schema.validate(dict(FIELDS, **changes))
        self.assertEqual(schema.validate({"layout": "page"}), {"verdict": "skip"})

    def test_item_preserves_body_and_hashes_whole_source(self):
        verdict = schema.validate(FIELDS)
        item, digest, source = ship_docs.item_from(DOCUMENT, verdict)
        self.assertEqual(item["content"], DOC.split("---\n")[-1])
        self.assertEqual(digest, hashlib.sha256(DOC.encode()).hexdigest())
        self.assertEqual(item["metadata"]["content_hash"], digest)
        self.assertEqual(source["kind"], "git")
        changed = dict(DOCUMENT, content=DOC.replace("status: draft", "status: accepted"))
        self.assertNotEqual(ship_docs.item_from(changed, verdict)[1], digest)
        self.assertEqual(ship_docs.item_from(DOCUMENT, dict(verdict, content="Override"))[0]["content"], "Override")

    def test_derive_boundary_rejects_malformed_verdicts_and_status_tags(self):
        for verdict in ([], {"verdict": "unknown"}, {"verdict": "ship", "document_id": 3}):
            with self.subTest(verdict=verdict), patch.object(ship_docs.subprocess, "run", return_value=Mock(stdout=json.dumps(verdict), stderr="")):
                with self.assertRaises(Error):
                    ship_docs.derive(DOCUMENT, "/schema/derive")
        for tags in ([], ["status:unknown"], ["status:draft", "status:accepted"]):
            with patch.object(ship_docs.subprocess, "run", return_value=Mock(stdout=json.dumps(dict(schema.validate(FIELDS), tags=tags)), stderr="")):
                self.assertEqual(ship_docs.derive(DOCUMENT, "/schema/derive")["verdict"], "refuse")


class ShipReportTest(unittest.TestCase):
    def setUp(self):
        self.store = report_store()
        self.report = ship_report.ScanReport(self.store, True)
        self.report.run["roots"] = [deepcopy(ROOT)]
        self.report.start()
        self.report.finish(0)
        self.success = self.store.get("bank")["last_success"]

    def test_partial_scan_preserves_full_success_and_status_is_read_only(self):
        report = ship_report.ScanReport(self.store, False)
        report.run["roots"] = [deepcopy(ROOT)]
        report.start()
        report.finish(0)
        self.assertEqual(self.store.get("bank")["last_success"], self.success)
        self.store.put.reset_mock()
        with patch.dict(os.environ, {"HINDSIGHT_MAX_SHIP_AGE": "7200"}):
            self.assertTrue(ship_report.status(self.store)["healthy"])
        self.store.put.assert_not_called()
        with patch.dict(os.environ, {"HINDSIGHT_MAX_SHIP_AGE": "0"}):
            self.assertFalse(ship_report.status(self.store)["healthy"])

    def test_incomplete_counts_and_failed_exit_never_advance_success(self):
        for key in ("failed", "refused", "gone", "incomplete"):
            with self.subTest(count=key):
                report = ship_report.ScanReport(self.store, True)
                report.run.update(counts={key: 1}, roots=[ROOT])
                report.start()
                report.finish(0)
                self.assertEqual(self.store.get("bank")["last_success"], self.success)
                self.assertEqual(report.run["status"], "incomplete")
        for roots, code in (([], 0), ([dict(ROOT, status="failed")], 0), ([ROOT], 5)):
            report = ship_report.ScanReport(self.store, True)
            report.run["roots"] = roots
            report.start()
            report.finish(code)
            self.assertEqual(report.run["status"], "incomplete")
            self.assertEqual(self.store.get("bank")["last_success"], self.success)

    def test_abandoned_or_unresolved_run_is_unhealthy_and_ownership_is_checked(self):
        self.store.list_documents.return_value = [{"document_id": "pending", "attempt": {"state": "pending"}}]
        self.assertEqual(ship_report.status(self.store)["unresolved_documents"], ["pending"])
        self.assertFalse(ship_report.status(self.store)["healthy"])
        self.store.list_documents.return_value = []
        abandoned = ship_report.ScanReport(self.store, True)
        abandoned.start()
        self.assertFalse(ship_report.status(self.store)["healthy"])
        with self.assertRaisesRegex(Error, "ownership changed"):
            self.report.finish(0)

    def test_corrupt_health_fails_closed(self):
        for field, value in (("last_success", []), ("latest_run", None),
                             ("last_success", {"finished_at": None})):
            with self.subTest(field=field):
                data = self.store.get("bank")
                data[field] = value
                self.store.put("bank", "", data)
                with self.assertRaises(Error):
                    ship_report.status(self.store)
                self.store.put("bank", "", dict(latest_run=self.success, latest_full_scan=self.success, last_success=self.success))


class ShipCLITest(unittest.TestCase):
    def setUp(self):
        self.store = report_store()
        self.api, self.ingestor = Mock(), Mock()
        self.api.inventory.return_value = []
        self.ingestor.retain.return_value = "shipped"
        self.snapshot = dict(roots=[deepcopy(ROOT)], documents=[deepcopy(DOCUMENT)], errors=0)
        for name, replacement in (("BeadsStore", Mock(return_value=self.store)),
                                  ("API", Mock(return_value=self.api)),
                                  ("Ingestor", Mock(return_value=self.ingestor)),
                                  ("require_writer", Mock()),
                                  ("snapshot", Mock(return_value=self.snapshot)),
                                  ("derive", Mock(return_value=schema.validate(FIELDS)))):
            p = patch.object(ship_docs, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"HINDSIGHT_API_URL": "https://fixture.invalid", "HINDSIGHT_BANK": "fixture"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def ship(self, *args, code=0):
        with patch.object(sys, "argv", ["ship_docs.py", *args, "/repo/docs"]), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(ship_docs.main(), code)
        self.output = out.getvalue()
        return self.store.get("bank")["latest_run"] if self.store.put.called else None

    def test_duplicate_ids_refuse_before_retain(self):
        self.snapshot["documents"].append(dict(DOCUMENT, relpath="docs/duplicate.md"))
        report = self.ship("--full-scan", code=1)
        self.ingestor.retain.assert_not_called()
        self.assertEqual(report["counts"]["refused"], 1)
        self.assertIn("multiple published files", report["documents"][0]["detail"])
        self.assertNotIn("last_success", self.store.get("bank"))

    def test_refused_document_keeps_id_and_is_not_gone(self):
        ship_docs.derive.return_value = dict(verdict="refuse", document_id="spec.fixture", reason="missing status")
        self.api.inventory.return_value = [dict(id="spec.fixture", document_metadata=dict(repo="repo", relpath="docs/spec.md"))]
        report = self.ship(code=1)
        self.assertEqual(report["documents"][0]["id"], "spec.fixture")
        self.assertEqual(report["counts"]["gone"], 0)
        self.ingestor.retain.assert_not_called()

    def test_gone_is_limited_to_scanned_root_and_blocks_full_success(self):
        self.api.inventory.return_value = [dict(id=name, document_metadata=dict(repo="repo", relpath=path))
                                           for name, path in (("gone", "docs/removed.md"), ("unwalked", "other-docs/file.md"))]
        report = self.ship("--full-scan")
        self.assertEqual(report["counts"]["gone"], 1)
        self.assertEqual(report["status"], "incomplete")
        self.assertNotIn("last_success", self.store.get("bank"))

    def test_snapshot_failure_stops_before_network_or_gone_detection(self):
        self.snapshot.update(errors=1, roots=[dict(ROOT, status="failed", detail="traversal failed")])
        report = self.ship("--full-scan", code=5)
        self.assertEqual(report["counts"]["incomplete"], 1)
        self.assertEqual(report["counts"]["gone"], 0)
        self.assertEqual(self.api.mock_calls, [])
        self.ingestor.retain.assert_not_called()

    def test_discovery_failure_is_persisted(self):
        report = self.ship("--full-scan", "--discovery-error", "cannot discover roots", code=5)
        self.assertEqual(report["roots"][0]["status"], "failed")
        self.assertEqual(self.store.get("bank")["latest_full_scan"]["status"], "incomplete")
        self.assertEqual(self.api.mock_calls, [])

    def test_boundary_errors_finalize_incomplete_with_correct_exit_code(self):
        for boundary, code in ((self.api.drain, 3), (self.api.inventory, 5), (self.ingestor.retain, 1)):
            with self.subTest(boundary=boundary):
                boundary.side_effect = Error("unavailable", code)
                report = self.ship(code=code)
                self.assertEqual(report["status"], "incomplete")
                boundary.side_effect = None
        self.api.inventory.return_value = [dict(id="spec.fixture", document_metadata="invalid")]
        self.assertEqual(self.ship(code=5)["status"], "incomplete")

    def test_dry_run_is_read_only_and_requires_receipt_not_just_bank_hash(self):
        item, digest, _ = ship_docs.item_from(DOCUMENT, schema.validate(FIELDS))
        self.api.inventory.return_value = [dict(id="spec.fixture", document_metadata=dict(content_hash=digest))]
        self.ship("--dry-run")
        self.assertIn("WOULD SHIP", self.output)
        self.store.put.assert_not_called()
        ship_docs.require_writer.assert_not_called()
        self.api.drain.assert_not_called()
        self.assertEqual(self.ingestor.mock_calls, [])
        self.store.put("document", "spec.fixture", dict(attempt={"state": "succeeded"}, last_success=dict(source_hash=digest, payload_hash=_payload_hash(item))))
        self.store.put.reset_mock()
        with patch.object(sys, "argv", ["ship_docs.py", "--dry-run", "/repo/docs"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ship_docs.main(), 0)
        self.assertIn("UNCHANGED", out.getvalue())
        self.store.put.assert_not_called()

    def test_recovery_precedes_inventory_and_force_only_suppresses_matching_reprocess(self):
        item, digest, _ = ship_docs.item_from(DOCUMENT, schema.validate(FIELDS))
        events = Mock()
        events.attach_mock(self.api, "api")
        events.attach_mock(self.ingestor, "ingestor")
        for reprocess in (False, True):
            with self.subTest(reprocess=reprocess):
                self.store.list_documents.return_value = [dict(document_id="spec.fixture", attempt=dict(reprocess=reprocess, source_hash=digest, payload_hash=_payload_hash(item)))]
                self.ingestor.recover.return_value = True
                events.reset_mock()
                report = self.ship("--reprocess")
                self.assertEqual(report["counts"]["recovered"], 1)
                self.assertEqual([c[0] for c in events.mock_calls], ["api.drain", "ingestor.recover", "api.inventory", "ingestor.retain"])
                self.assertEqual(self.ingestor.retain.call_args.kwargs["force"], not reprocess)


class RequestAndDiscoveryTest(unittest.TestCase):
    def test_request_round_trip_preserves_literal_arguments_and_revalidates(self):
        args = ["--ref", "branch with spaces", "docs with spaces/$(do-not-execute)"]
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["request", "encode", *args]), redirect_stdout(io.StringIO()) as out:
            request.main()
        encoded = out.getvalue().strip()
        normalized = json.loads(base64.b64decode(encoded))
        self.assertEqual(normalized, ["--ref", args[1], str(Path(args[2]).absolute())])
        with patch.dict(os.environ, {"HINDSIGHT_WRITER": "archivist", "GC_SESSION_ID": "fixture"}), patch.object(sys, "argv", ["request", "execute", encoded]), patch.object(request.os, "execv") as execute:
            request.main()
        self.assertEqual(execute.call_args.args[1][1:], normalized)
        for args in (["--full-scan"], ["--ref"], ["--api", "https://user:fixture-secret@fixture.invalid"]):
            with self.subTest(args=args), self.assertRaises(ValueError) as error:
                request.normalize(args)
            self.assertNotIn("fixture-secret", str(error.exception))

    def test_connection_precedence_config_and_no_default_server(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HINDSIGHT_CONFIG": str(Path(tmp) / "connection.toml")}, clear=True):
            with self.assertRaises(ValueError):
                connection.resolve()
            Path(os.environ["HINDSIGHT_CONFIG"]).write_text('api_url = "https://configured.invalid/"\napi_key = "fixture-token"\n')
            self.assertEqual(connection.resolve(), dict(api="https://configured.invalid", key="fixture-token"))
            os.environ.update(HINDSIGHT_API="https://alias.invalid", HINDSIGHT_API_URL="https://unused.invalid", HINDSIGHT_API_KEY="override-key")
            self.assertEqual(connection.resolve()["api"], "https://alias.invalid")
            self.assertEqual(connection.resolve("https://override.invalid/"), dict(api="https://override.invalid", key="override-key"))

    def test_discovery_includes_absent_docs_but_rejects_missing_or_parent_checkouts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            city = Path(tmp).resolve()
            rig = city / "rig"
            rig.mkdir()
            registry = dict(rigs=[dict(name="repo", path=str(rig))])
            with patch.object(ship_docs.subprocess, "run", return_value=Mock(stdout=json.dumps(registry))), patch.object(ship_docs, "_git", side_effect=[str(rig).encode(), b"true"]):
                self.assertEqual(ship_docs.discover_roots(str(city)), [str(rig / "docs"), str(city / "docs")])
            for path in (rig, city / "missing"):
                registry["rigs"][0]["path"] = str(path)
                with patch.object(ship_docs.subprocess, "run", return_value=Mock(stdout=json.dumps(registry))), patch.object(ship_docs, "_git", return_value=str(city).encode()):
                    with self.assertRaisesRegex(Error, "own Git checkout"):
                        ship_docs.discover_roots(str(city))


class PackShellTest(ShellFixture):
    def test_ship_happy_path_and_shared_receipt_skip(self):
        docs = self.published_docs()
        self.env["HINDSIGHT_API_KEY"] = "fixture-secret"
        self.run_script(SCRIPTS / "ship-docs.sh", "--full-scan", docs)
        payload = self.posts()[0]["payload"]
        self.assertIs(payload["async"], True)
        self.assertEqual(payload["items"][0]["content"], DOC.split("---\n")[-1])
        receipt = self.state("spec.fixture")["last_success"]
        self.assertEqual(receipt["operation_id"], payload["operation_id"])
        for call in self.calls("curl"):
            self.assertIn("Authorization: Bearer fixture-secret", call["stdin"])
            self.assertNotIn("fixture-secret", str(call["args"]))
        for post in self.posts():
            self.assertFalse(Path(post["args"][post["args"].index("--data-binary") + 1][1:]).exists())
        self.run_script(SCRIPTS / "ship-docs.sh", docs)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.state()["latest_run"]["counts"]["skipped"], 1)
        self.assertEqual(self.state("spec.fixture")["last_success"], receipt)
        self.assertEqual(self.state()["last_success"]["status"], "ok")
        before = (self.root / "beads.json").read_text()
        network_calls = len(self.calls("curl"))
        self.run_script(PACK / "commands/status/run.sh")
        self.assertEqual(len(self.calls("curl")), network_calls)
        self.assertEqual((self.root / "beads.json").read_text(), before)

    def test_manual_queue_preserves_literal_request_and_propagates_failure(self):
        self.env.pop("HINDSIGHT_WRITER")
        self.env.pop("GC_SESSION_ID")
        path = "docs with spaces/$(do-not-execute)"
        self.run_script(PACK / "commands/ship/run.sh", "--ref", "branch with spaces", path)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"][:3], ["sling", "hindsight.archivist", "mol-hindsight-ship"])
        args = json.loads(base64.b64decode(calls[0]["args"][-1].split("=", 1)[1]))
        self.assertIn(str(self.root / path), args)
        self.assertIn("branch with spaces", args)
        self.config["sling_error"] = True
        result = self.run_script(PACK / "commands/ship/run.sh", code=1)
        self.assertNotIn("Queued ship request", result.stdout)
        self.assertFalse(self.calls("curl"))

    def test_partial_matching_hash_retries_original_operation(self):
        docs = self.published_docs()
        self.config["operation_status"] = "failed"
        self.run_script(SCRIPTS / "ship-docs.sh", "--full-scan", docs, code=1)
        op = self.state("spec.fixture")["attempt"]["operation_id"]
        server = json.loads((self.root / "server.json").read_text())
        digest = hashlib.sha256(DOC.encode()).hexdigest()
        self.assertEqual(server["documents"][0]["document_metadata"]["content_hash"], digest)
        self.assertNotIn("last_success", self.state("spec.fixture"))
        self.assertNotIn("last_success", self.state())
        self.config.clear()
        self.run_script(SCRIPTS / "ship-docs.sh", "--full-scan", docs)
        self.assertEqual(len(self.posts()), 2)
        self.assertTrue(any(a.endswith(f"/operations/{op}/retry") for a in self.posts()[-1]["args"]))
        self.assertEqual(self.state("spec.fixture")["last_success"]["operation_id"], op)
        self.assertEqual(self.state()["latest_run"]["counts"]["recovered"], 1)
        self.assertEqual(self.state()["last_success"]["status"], "ok")

    def test_schema_shell_frontmatter_and_null_status_contract(self):
        for text, verdict in (("# ordinary markdown", "skip"), ("---\nlayout: page\n---\nhello", "skip"), ("---\nid: incomplete", "refuse")):
            with self.subTest(text=text):
                result = self.run_script(PACK / "schemas/docs/derive", input=text)
                self.assertEqual(json.loads(result.stdout)["verdict"], verdict)
        raw = "---\nhindsight:\n  id: raw.fixture\n  strategy: design-record\n  tags: [team:eng%s]\n---\nBody"
        for status, verdict in (("", "refuse"), (", status:draft", "ship")):
            result = self.run_script(PACK / "schemas/null/derive", input=raw % status)
            self.assertEqual(json.loads(result.stdout)["verdict"], verdict)

    def test_audit_pages_registry_and_config_drift(self):
        # Pagination needs 501 entries, not 501 distinct tags to audit with jq.
        self.config.update(tags=["domain:abc"] * 500 + ["repo:unknown"], auto=False)
        result = self.run_script(SCRIPTS / "bank-maintain.sh", "--skip-consolidate", code=2)
        self.assertIn("UNKNOWN-RIG   repo:unknown", result.stdout)
        self.assertIn("enable_auto_consolidation=false", result.stdout)
        self.assertEqual([c["args"][-1] for c in self.calls("hindsight") if "tag" in c["args"]], ["0", "500"])

    def test_empty_audit_and_unavailable_registry(self):
        self.run_script(SCRIPTS / "bank-maintain.sh", "--skip-consolidate")
        self.config["rig_error"] = True
        result = self.run_script(SCRIPTS / "bank-maintain.sh", "--skip-consolidate", code=2)
        self.assertIn("AUDIT-INCOMPLETE", result.stdout)


if __name__ == "__main__":
    unittest.main()
