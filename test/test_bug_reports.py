"""Report-only bug fixes assess Beads proposals without inventing intent docs."""
import importlib.util
from pathlib import Path
import sys
import json
import subprocess
import tempfile
import unittest

PACK = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bug_report_schema", PACK / "schemas/docs/validate.py")
schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schema)
sys.path.insert(0, str(PACK / "assets/scripts"))
import ship_docs
import publication


class BugReportTests(unittest.TestCase):
    def test_capability_probe_requires_no_repository_or_bank(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, expected in (("docs", True), ("null", False)):
                result = subprocess.run([sys.executable, str(PACK / "assets/scripts/check_docs.py"),
                                         "--capabilities", "--schema", str(PACK / "schemas" / name)],
                                        cwd=tmp, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"build_report_assesses_beads": expected, "ship_queue_json": True})

    def report(self):
        return dict(schema_version=2, id="app.build-report.bug1", type="build-report", title="Fix incorrect result",
                    status="draft", source="agent", scope="repo", repos=["app"],
                    updated_at="2026-10-24T00:00:00Z", outcome="passed", assesses=[],
                    assesses_beads=[dict(store="rig:app", id="app-bug1", proposal_sha256="a" * 64)],
                    code=[dict(repo="app", commit="b" * 40)])

    def test_report_without_intent_documents_round_trips_publication(self):
        doc = self.report()
        verdict = schema.validate(doc)
        self.assertEqual(verdict["verdict"], "ship")
        self.assertIn("tracked-work assessment", verdict["context"])
        source = dict(repository="https://example.test/app", relpath="docs/report.md", commit="c" * 40,
                      ref="refs/heads/main")
        candidate = ship_docs.candidate_from(source, verdict, "d" * 64, "app")
        record = publication.publication_record(candidate, None, None, "now")
        self.assertEqual(record["assesses_beads"], doc["assesses_beads"])
        decisions, _ = publication.plan({doc["id"]: candidate}, {}, {}, set())
        self.assertEqual(decisions[doc["id"]].action, "publish")

    def test_no_empty_or_malformed_assessment_escape(self):
        for beads in ([], None, [dict(id="app-bug1")],
                      [dict(store="app", id="app-bug1", proposal_sha256="a" * 64)],
                      [dict(store="rig:app", id="app-bug1", proposal_sha256=123)]):
            with self.subTest(beads=beads), self.assertRaises(ValueError):
                schema.validate(dict(self.report(), assesses_beads=beads))

    def test_duplicate_beads_and_non_report_use_rejected(self):
        doc = self.report()
        with self.assertRaises(ValueError):
            schema.validate(dict(doc, assesses_beads=doc["assesses_beads"] * 2))
        with self.assertRaises(ValueError):
            schema.validate(dict(doc, type="spec"))

    def test_ordinary_document_assessments_unchanged(self):
        doc = self.report()
        del doc["assesses_beads"]
        doc["assesses"] = [dict(id="app.spec.original", fingerprint="e" * 64)]
        self.assertEqual(schema.validate(doc)["verdict"], "ship")


if __name__ == "__main__":
    unittest.main()
