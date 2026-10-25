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
    {
        "id": "recorded-conditional-assessment-and-next-step",
        "query": "What assessment and next step were recorded about the shared reference interface in the February review? Return the assessor's first name, whether feasibility was assessed conditionally, whether the condition is the tenant-isolation review, whether the recommended next step is a sandbox trial after that review, and whether the interface or trial was approved.",
        "expected": {"assessor": "Casey", "conditionally_feasible": True, "condition_is_tenant_isolation_review": True, "recommended_sandbox_trial_after_review": True, "approved": False},
        "tags": "repo:svc-guidance",
    },
    {
        "id": "latest-holistic-stakeholder-position",
        "query": "What is Morgan's current position after the March meeting? Return enterprise and small-account delivery modes as batch or streaming, whether Morgan favors weekend support for each, and whether the March comments revised delivery modes.",
        "expected": {"enterprise_delivery": "streaming", "small_account_delivery": "batch", "enterprise_weekend_support": True, "small_account_weekend_support": False, "march_revised_delivery_modes": False},
        "tags": "repo:svc-guidance",
    },
    {
        "id": "name-does-not-license-scaling-guidance",
        "query": "How can I scale the svc-inventory Kubernetes deployment to three replicas? Return the bare deployment name, whether a scaling procedure is recorded for it, and the scaling command. Use null for the command if none is recorded.",
        "expected": {"deployment": "payments-api", "scaling_procedure_recorded": False, "recorded_scaling_command": None},
        "label_fields": ["deployment"],
        "tags": "repo:svc-inventory",
    },
    {
        "id": "retained-runbook-remains-retrievable",
        "query": "What scaling procedure is recorded for svc-runbook? Return its exact command for three replicas, whether it is sandbox-only, and whether execution is recorded.",
        "expected": {"command": "kubectl scale deployment/runbook-worker --replicas=3 --namespace=sandbox", "sandbox_only": True, "execution_recorded": False},
        "tags": "repo:svc-runbook",
    },
    {
        "id": "status-conflict-remains-visible",
        "query": "Are quarterly architecture reviews a settled convention for svc-guidance? Return the status label of convention.guidance.status-conflict.0001, whether its text describes a draft proposal, and whether its status label and source wording agree.",
        "expected": {"status_label": "accepted", "text_describes_draft_proposal": True, "status_and_text_agree": False},
        "label_fields": ["status_label"],
        "tags": "repo:svc-guidance",
    },
    {
        "id": "unattributed-recommendation-remains-retrievable",
        "query": "What recommendation was recorded in the March notes before the next planning meeting? Return whether it calls for a documentation audit, its author's name, and whether adoption is recorded. Use null when the author is not identified.",
        "expected": {"documentation_audit_recommended": True, "author": None, "adoption_recorded": False},
        "tags": "repo:svc-guidance",
    },
    {
        "id": "requirements-do-not-endorse-proposed-design",
        "query": "Can we use a path-only terraform-data-only interface for FRED credentials and guarantee no secret enters Terraform state? Return whether the record establishes that design as approved, whether it establishes that guarantee for the design, whether it requires runtime decryption, and whether it prohibits secrets in state. Return the recorded discovery preference order as a comma-separated list, no spaces, using the tokens explicit, provider, shared.",
        "expected": {"design_approved": False, "design_guarantee_established": False, "runtime_decryption_required": True, "secrets_in_state_prohibited": True, "discovery_rank": "provider,shared,explicit"},
        "tags": "repo:svc-credentials",
    },
    {
        "id": "documented-rule-applies-to-recorded-repository",
        "query": "What database convention applies to svc-reports? Return the default service database product without its version, whether a MySQL vendor exception requires an accepted ADR, and whether the record establishes this rule for svc-reports even though the database convention does not name that repository individually.",
        "expected": {"database": "PostgreSQL", "mysql_requires_accepted_adr": True, "rule_applies_to_svc_reports": True},
        "tags": "repo:svc-reports,scope:platform",
    },
]


def matches_expected(case, actual):
    if not isinstance(actual, dict):
        return False
    normalized = dict(actual)
    for field in case.get("label_fields", []):
        if isinstance(normalized.get(field), str):
            normalized[field] = normalized[field].strip().strip("`").casefold()
    expected = dict(case["expected"])
    for field in case.get("label_fields", []):
        expected[field] = expected[field].casefold()
    return normalized == expected


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
        passed = matches_expected(case, actual) and error is None
        results.append(dict(id=case["id"], passed=passed, expected=case["expected"], actual=actual, error=error))
        print(f"{'PASS' if passed else 'FAIL'} {case['id']}", flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2))
    print(f"Evidence and results: {output}")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
