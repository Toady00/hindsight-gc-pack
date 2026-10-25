"""Offline checks of live-evaluation scoring, without contacting a bank."""
import unittest

try:
    from .evaluate import CASES, matches_expected
except ImportError:
    from evaluate import CASES, matches_expected


class EvaluationScoringTest(unittest.TestCase):
    def test_label_formatting_does_not_mask_wrong_identity_or_missing_fields(self):
        case = next(c for c in CASES if c["id"] == "name-does-not-license-scaling-guidance")
        answer = dict(case["expected"], deployment=" `PAYMENTS-API` ")
        self.assertTrue(matches_expected(case, answer))
        answer["deployment"] = "other-deployment"
        self.assertFalse(matches_expected(case, answer))
        self.assertFalse(matches_expected(case, {"deployment": "payments-api"}))
        self.assertFalse(matches_expected(case, None))

    def test_runbook_command_remains_exact(self):
        case = next(c for c in CASES if c["id"] == "retained-runbook-remains-retrievable")
        self.assertTrue(matches_expected(case, dict(case["expected"])))
        for command in (case["expected"]["command"].upper(),
                        case["expected"]["command"].replace("--replicas=3", "--replicas=4")):
            with self.subTest(command=command):
                self.assertFalse(matches_expected(case, dict(case["expected"], command=command)))


if __name__ == "__main__":
    unittest.main()
