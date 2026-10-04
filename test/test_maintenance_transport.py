"""Transport retry policy for nightly maintenance HTTP calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PACK = Path(__file__).resolve().parents[1]


class MaintenanceTransportTest(unittest.TestCase):
    def run_curl(self, outcomes, function="hs_http_post_consolidate https://api.invalid/consolidate", api_key=""):
        with tempfile.TemporaryDirectory(prefix="hindsight-maintenance-") as temp:
            root = Path(temp)
            (root / "outcomes").write_text(json.dumps(outcomes))
            curl = root / "curl"
            curl.write_text('''#!/usr/bin/env python3
import json, os, sys
import time
from pathlib import Path
root = Path(os.environ["MOCK_ROOT"])
state = root / "count"
n = int(state.read_text()) if state.exists() else 0
state.write_text(str(n + 1))
outcome = json.loads((root / "outcomes").read_text())[min(n, len(json.loads((root / "outcomes").read_text())) - 1)]
args = sys.argv[1:]
output = args[args.index("--output") + 1]
time.sleep(outcome.get("sleep", 0))
Path(output).write_text(outcome.get("body", '{"operation_id":"op-1"}'))
config_path = args[args.index("--config") + 1]
capture = {"argv": args, "stdin": sys.stdin.read(), "config": Path(config_path).read_text()}
(root / "capture").write_text(json.dumps(capture))
print(outcome.get("status", ""), end="")
sys.exit(outcome.get("code", 0))
''')
            curl.chmod(0o755)
            body, error = root / "body", root / "error"
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", MOCK_ROOT=temp,
                       HS_HTTP_BODY=str(body), HS_HTTP_ERROR=str(error), HINDSIGHT_API_KEY=api_key)
            command = f'source "$1"; {function}'
            result = subprocess.run(["bash", "-c", command, "bash", str(PACK / "assets/scripts/common.sh")],
                                    env=env, text=True, capture_output=True, timeout=12)
            return result, int((root / "count").read_text()), json.loads((root / "capture").read_text())

    def test_connection_establishment_failure_is_retried(self):
        result, calls, _ = self.run_curl([{"code": 7}, {"status": "200", "body": '{"operation_id":"op-1"}'}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"operation_id":"op-1"', result.stdout)
        self.assertEqual(calls, 2)

    def test_ambiguous_timeout_is_not_retried(self):
        result, calls, _ = self.run_curl([{"code": 28}])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, 1)

    def test_failed_attempt_body_is_cleared_before_next_attempt(self):
        result, calls, _ = self.run_curl([
            {"code": 7, "body": "stale response body"},
            {"code": 28, "body": "latest attempt body"},
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, 2)
        self.assertNotIn("stale response body", result.stderr)
        self.assertIn("latest attempt body", result.stderr)

    def test_http_rejection_is_not_retried(self):
        result, calls, _ = self.run_curl([{"code": 22, "status": "503", "body": "service unavailable"}])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, 1)

    def test_transient_read_http_failure_retries_with_same_request(self):
        result, calls, _ = self.run_curl(
            [{"code": 22, "status": "503", "body": "temporarily unavailable"},
             {"status": "200", "body": '{"total":0,"operations":[]}'}],
            "hs_http_read https://api.invalid/operations",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"total":0,"operations":[]}')
        self.assertEqual(calls, 2)

    def test_deadline_expiry_on_retry_emits_last_attempt_diagnostics(self):
        result, calls, _ = self.run_curl(
            [{"code": 22, "status": "503", "body": "last status body",
              "sleep": 1.2}],
            "HS_HTTP_DEADLINE=$((SECONDS+1)) HS_HTTP_ALLOW_INITIAL=true hs_http_read https://api.invalid/operations",
        )
        self.assertEqual(result.returncode, 28)
        self.assertEqual(calls, 1)
        self.assertIn("last status body", result.stderr)

    def test_api_key_uses_curl_config_fd_not_argv_or_stdin(self):
        secret = "maintenance-fixture-secret"
        result, calls, capture = self.run_curl([{"status": "200"}], api_key=secret)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, 1)
        config_index = capture["argv"].index("--config") + 1
        self.assertTrue(capture["argv"][config_index].startswith("/dev/fd/"))
        self.assertIn(f"Authorization: Bearer {secret}", capture["config"])
        self.assertNotIn(secret, " ".join(capture["argv"]))
        self.assertEqual(capture["stdin"], "")


if __name__ == "__main__":
    unittest.main()
