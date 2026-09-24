"""Offline semantic-lint contracts. Model quality belongs to the opt-in evaluation."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import urllib.error

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK / "assets/scripts"))
import semantic_lint as lint
from test import evaluate_semantic_lint as evaluation

FIXTURES = json.loads((PACK / "test/fixtures/semantic-lint-cases.json").read_text())


def metadata(**overrides):
    return dict(FIXTURES["frontmatter"], id="spec.fixture", **overrides)


def response(request, value=0.01):
    return {"model": lint.MODEL, "answers": {key: {"type": "noul", "noul": value}
            for key in request["questions"]}, "usage": {"input_tokens": 100}}


class SemanticLintTest(unittest.TestCase):
    def test_fixture_coverage_matches_applicable_questions(self):
        for case in FIXTURES["cases"]:
            with self.subTest(case=case["id"]):
                fm = metadata()
                fm.update(case.get("frontmatter", {}))
                lint.schema_validate(fm)
                request = lint.build_request(fm, case["body"], lint.MODEL)
                self.assertEqual(set(request["questions"]), set(case["expected"]))
                self.assertEqual(request["state"]["document"]["body"], case["body"])
                self.assertNotIn("updated_at", request["state"]["document"]["frontmatter"])

    def test_frontmatter_parser_and_body_preservation(self):
        fm = metadata()
        raw = b"---\r\nid: spec.fixture\r\n---\r\n# Example\r\n\r\nStatus: DRAFT\r\n"
        with patch.object(lint.subprocess, "run", return_value=Mock(returncode=0, stdout=json.dumps(fm))) as parse:
            parsed, body, line = lint.parse_document(raw)
        self.assertEqual(parsed, fm)
        self.assertEqual(body, "# Example\r\n\r\nStatus: DRAFT\r\n")
        self.assertEqual(line, 4)
        self.assertEqual(parse.call_args.kwargs["input"], "id: spec.fixture\r\n")

    def test_bad_input_refused_before_parsing(self):
        for raw in (b"no frontmatter", b"---\nid: x", b"\xff"):
            with self.subTest(raw=raw[:20]), patch.object(lint.subprocess, "run") as parse:
                with self.assertRaises(lint.LintError):
                    lint.parse_document(raw)
                parse.assert_not_called()

    def test_invalid_yaml_schema_empty_body_and_parser_unavailability(self):
        raw = b"---\nid: x\n---\nBody\n"
        for result in (Mock(returncode=1), Mock(returncode=0, stdout="null"),
                       Mock(returncode=0, stdout='{"id":"x"}'), Mock(returncode=0, stdout="not JSON")):
            with patch.object(lint.subprocess, "run", return_value=result), self.assertRaises(lint.LintError):
                lint.parse_document(raw)
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("yq", 10)):
            with patch.object(lint.subprocess, "run", side_effect=failure), self.assertRaises(lint.LintError):
                lint.parse_document(raw)
        with patch.object(lint.subprocess, "run", return_value=Mock(returncode=0, stdout=json.dumps(metadata()))):
            with self.assertRaisesRegex(lint.LintError, "body is empty"):
                lint.parse_document(b"---\nid: x\n---\n")

    def test_large_document_preview_preserves_qualifications_at_end(self):
        body = "Observed behavior and background.\n" * 2000 + "\nThis document is accepted; DRAFT refers to an earlier revision.\n"
        raw = ("---\n" + json.dumps(metadata()) + "\n---\n" + body).encode()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "large.md"
            path.write_bytes(raw)
            with patch.object(lint.subprocess, "run", return_value=Mock(returncode=0, stdout=json.dumps(metadata()))), \
                    patch.object(lint, "evaluate") as service:
                report, code = lint.run([path], dry_run=True)
        self.assertEqual(code, 0)
        item = report["documents"][0]
        self.assertEqual(item["request"]["state"]["document"]["body"], body)
        self.assertEqual(item["document_bytes"], len(raw))
        self.assertGreater(item["request_bytes"], 28_000)
        self.assertEqual(item["status"], "preview")
        service.assert_not_called()

    def test_probabilities_uncertainty_and_invalid_responses(self):
        request = lint.build_request(metadata(), "body", lint.MODEL)
        for probability, expected in ((0, "clear"), (0.2, "clear"), (0.5, "uncertain"), (0.8, "finding"), (1, "finding")):
            self.assertTrue(all(c["status"] == expected for c in lint.classify_response(response(request, probability), request, 0.8)))
        for probability in (True, "0.9", float("nan"), float("inf"), -0.1, 1.1, None):
            with self.subTest(probability=probability), self.assertRaises(lint.LintError):
                lint.classify_response(response(request, probability), request, 0.8)
        for bad in (None, {}, {"model": "jev", "answers": {}},
                    {"model": "jev", "answers": {"extra": {}}}):
            with self.assertRaises(lint.LintError):
                lint.classify_response(bad, request, 0.8)
        for usage in (None, {"input_tokens": float("nan")}, {"output_tokens": -1}):
            bad = response(request)
            bad["usage"] = usage
            with self.assertRaises(lint.LintError):
                lint.classify_response(bad, request, 0.8)

    def test_no_key_preview_and_preflight_errors_never_call_service(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.md"
            path.write_text("fixture bytes")
            with patch.object(lint, "parse_document", return_value=(metadata(), "Body", 4)), \
                    patch.object(lint, "evaluate") as service, patch.dict(os.environ, {}, clear=True):
                report, code = lint.run([path], dry_run=True)
                self.assertEqual(code, 0)
                self.assertEqual(report["documents"][0]["status"], "preview")
                self.assertIn("request", report["documents"][0])
                self.assertEqual(len(report["documents"][0]["sha256"]), 64)
                report, code = lint.run([path])
                self.assertEqual(code, 2)
                self.assertIn("TYPESAFE_API_KEY", report["documents"][0]["error"])
                report, code = lint.run([path, Path(root) / "absent"], dry_run=True)
                self.assertEqual(code, 2)
                service.assert_not_called()

    def test_no_applicable_rules_is_distinct_and_needs_no_key(self):
        fm = metadata()
        fm.update(status="draft", scope="platform")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.md"
            path.write_text("fixture bytes")
            with patch.object(lint, "parse_document", return_value=(fm, "Body", 4)), \
                    patch.object(lint, "evaluate") as service, patch.dict(os.environ, {}, clear=True):
                report, code = lint.run([path])
            self.assertEqual(code, 0)
            self.assertEqual(report["documents"][0]["status"], "not-applicable")
            service.assert_not_called()

    def test_live_report_and_failure_stops_remaining_files(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / name for name in ("one.md", "two.md")]
            for path in paths:
                path.write_text("fixture bytes")
            request = lint.build_request(metadata(), "Body", lint.MODEL)
            with patch.object(lint, "parse_document", return_value=(metadata(), "Body", 4)), \
                    patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-only"}), \
                    patch.object(lint, "evaluate", return_value=(response(request, 0.5), 0.1)) as service:
                report, code = lint.run(paths)
                self.assertEqual(code, 1)
                self.assertEqual(service.call_count, 2)
                self.assertEqual(report["documents"][0]["model"], lint.MODEL)
                self.assertEqual(report["documents"][0]["elapsed_seconds"], 0.1)
                service.reset_mock()
                service.side_effect = lint.LintError("TypeSafe HTTP 429")
                report, code = lint.run(paths)
                self.assertEqual(code, 2)
                self.assertEqual(service.call_count, 1)
                self.assertEqual([i["status"] for i in report["documents"]], ["error", "not-run"])

    def test_http_contract_errors_do_not_echo_response_body_or_retry(self):
        request = lint.build_request(metadata(), "Body", lint.MODEL)
        wire_response = io.BytesIO(json.dumps(response(request)).encode())
        opener = Mock()
        opener.open.return_value = wire_response
        with patch.object(lint.urllib.request, "build_opener", return_value=opener):
            result, elapsed = lint.evaluate(request, "test-secret", 12)
        self.assertEqual(result["model"], lint.MODEL)
        sent = opener.open.call_args.args[0]
        self.assertEqual(sent.full_url, lint.ENDPOINT)
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(json.loads(sent.data), request)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
        self.assertGreaterEqual(elapsed, 0)
        for status in (401, 422, 429, 529, 302):
            opener.reset_mock()
            opener.open.side_effect = urllib.error.HTTPError(lint.ENDPOINT, status, "remote secret", {}, io.BytesIO(b"test-secret"))
            with patch.object(lint.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(lint.LintError) as caught:
                    lint.evaluate(request, "test-secret", 12)
            self.assertIn(str(status), str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(opener.open.call_count, 1)
        self.assertIsNone(lint.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_cli_rejects_invalid_thresholds_and_timeout(self):
        for args in (("--threshold", "nan"), ("--threshold", "0.5"), ("--threshold", "1.1"), ("--timeout", "inf")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                lint.main([*args, "file.md"])
            self.assertEqual(caught.exception.code, 2)

    def test_evaluation_does_not_count_uncertainty_or_missing_checks_as_pass(self):
        report = {"documents": [{"checks": [{"rule": "a", "status": "finding"},
                                             {"rule": "b", "status": "uncertain"},
                                             {"rule": "c", "status": "clear"}]}]}
        counts = evaluation.grade(report, [{"id": "test", "expected": {"a": False, "b": False, "c": True, "d": True}}])
        self.assertEqual(counts, dict(correct=0, false_positive=1, false_negative=1, uncertain=1, unscored=1))


if __name__ == "__main__":
    unittest.main()
