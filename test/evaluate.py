#!/usr/bin/env python3
"""Optional live answer-quality checks, restricted to explicitly named test banks."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

PACK = Path(__file__).resolve().parents[1]
CASES = [
    {
        "id": "draft-is-not-adopted",
        "query": "For svc-widgets, return the accepted specification's transport as its short AWS service name, the proposed replacement as its short service name, whether the replacement is adopted, the accepted maxReceiveCount, and accepted visibility timeout in seconds.",
        "expected": {"specified_transport": "SQS", "proposed_transport": "EventBridge", "replacement_adopted": False, "max_receive_count": 5, "visibility_seconds": 90},
        "tags": "repo:svc-widgets,scope:platform",
    },
    {
        "id": "thinking-and-ranked-preferences",
        "query": "Return the platform's default service database product name without its version, its major version as an integer, whether DuckDB is an adopted analytics exception, whether SQLite is allowed for service datastores, and whether a MySQL vendor exception requires an accepted ADR.",
        "expected": {"database": "PostgreSQL", "major_version": 16, "duckdb_adopted": False, "sqlite_service_datastore": False, "mysql_exception_requires_adr": True},
        "tags": "scope:platform",
    },
    {
        "id": "supersession-and-scope",
        "query": "For svc-reports, return the current source-of-truth format as JSON or Markdown, the exact schema version, whether the Markdown decision is superseded, and whether this establishes report storage for every repository rather than just svc-reports.",
        "expected": {"format": "JSON", "schema_version": "report.v2", "markdown_superseded": True, "establishes_all_repos": False},
        "tags": "repo:svc-reports,scope:platform",
    },
    {
        "id": "do-not-invent-observation-or-reaffirmation",
        "query": "Does this corpus contain a code survey or build report independently verifying that svc-widgets currently implements its accepted transport specification? Also return the date of any explicit reaffirmation event for spec.eventing.transport.0001 after its original August 1 revision. A later competing proposal does not establish reaffirmation. Use null if no reaffirmation event is documented.",
        "expected": {"implementation_independently_verified": False, "reaffirmed_on": None},
        "tags": "repo:svc-widgets,scope:platform",
    },
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bank", help="Existing fixture bank named hindsight-eval-*")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.bank.startswith("hindsight-eval-") or args.bank == "hindsight-eval-":
        parser.error("use an explicit hindsight-eval-* fixture bank")
    output = args.output or Path(tempfile.mkdtemp(prefix="hindsight-eval-"))
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for case in CASES:
        properties = {}
        for field, value in case["expected"].items():
            kind = "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "string"
            properties[field] = {"type": ["string", "null"] if value is None else kind}
        schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
        schema_path = output / f"{case['id']}.schema.json"
        schema_path.write_text(json.dumps(schema))
        command = [str(PACK / "assets/scripts/hindsight-read.sh"), "-o", "json", "memory", "reflect", args.bank,
                   case["query"], "--schema", str(schema_path), "--tags", case["tags"], "--tags-match", "any_strict", "--budget", "mid", "--max-tokens", "1024", "--include-facts"]
        run = subprocess.run(command, capture_output=True, text=True, timeout=300)
        (output / f"{case['id']}.response.json").write_text(run.stdout)
        (output / f"{case['id']}.stderr.txt").write_text(run.stderr)
        actual, error = None, None
        try:
            if run.returncode:
                raise ValueError(f"read failed with exit {run.returncode}")
            response = json.loads(run.stdout)
            actual = response.get("structured_output")
            if actual is None:
                actual = json.loads(response["text"])
        except (ValueError, KeyError, TypeError) as failure:
            error = str(failure)
        passed = actual == case["expected"] and error is None
        results.append(dict(id=case["id"], passed=passed, expected=case["expected"], actual=actual, error=error))
        print(f"{'PASS' if passed else 'FAIL'} {case['id']}", flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2))
    print(f"Evidence and results: {output}")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
