#!/usr/bin/env python3
"""Opt-in Jev evaluation on labeled documents. --dry-run makes no API calls."""
import argparse
import json
import math
from pathlib import Path
import sys
import tempfile

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK / "assets/scripts"))
import semantic_lint


def grade(report, cases):
    counts = dict(correct=0, false_positive=0, false_negative=0, uncertain=0, unscored=0)
    for item, case in zip(report["documents"], cases):
        item["case_id"] = case["id"]
        item["expected"] = case["expected"]
        checks = {check["rule"]: check for check in item.get("checks", [])}
        outcomes = {}
        for rule, expected in case["expected"].items():
            status = checks.get(rule, {}).get("status")
            if status is None:
                outcome = "unscored"
            elif status == "uncertain":
                outcome = "uncertain"
            elif (status == "finding") == expected:
                outcome = "correct"
            else:
                outcome = "false_negative" if expected else "false_positive"
            outcomes[rule] = outcome
            counts[outcome] += 1
        item["outcomes"] = outcomes
    report["evaluation"] = counts
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default=semantic_lint.MODEL)
    parser.add_argument("--threshold", type=float, default=0.8)
    args = parser.parse_args(argv)
    if not math.isfinite(args.threshold) or not 0.5 < args.threshold <= 1:
        parser.error("--threshold must be greater than 0.5 and at most 1")
    fixtures = json.loads((PACK / "test/fixtures/semantic-lint-cases.json").read_text())
    with tempfile.TemporaryDirectory(prefix="hindsight-semantic-eval-") as directory:
        paths = []
        for case in fixtures["cases"]:
            path = Path(directory) / (case["id"] + ".md")
            frontmatter = dict(fixtures["frontmatter"], **case.get("frontmatter", {}))
            frontmatter["id"] = "spec.lint-eval." + case["id"]
            # JSON is valid YAML. Use the same production parser and validator.
            path.write_text("---\n" + json.dumps(frontmatter) + "\n---\n" + case["body"])
            paths.append(path)
        report, code = semantic_lint.run(paths, dry_run=args.dry_run, model=args.model, threshold=args.threshold)
        counts = grade(report, fixtures["cases"])
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    if code == 2 or args.dry_run:
        return code
    # A correctly detected intentional violation passes the evaluation even
    # though the linter itself returns 1. Unknown/missed checks never pass.
    return 1 if any(counts[k] for k in ("false_positive", "false_negative", "uncertain", "unscored")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
