#!/usr/bin/env python3
"""The docs contract, shared by document and bank-native writers.

``validate`` checks parsed frontmatter. ``fingerprint`` identifies a revision
for the publication lifecycle: the exact file bytes with only the values of the
top-level ``status`` and ``updated_at`` lines masked. Everything else, including
``source``, ``id``, comments, and the body, stays significant.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

STRATEGIES = {
    **dict.fromkeys(("adr", "spec", "hld"), "design-record"),
    **dict.fromkeys(("prd", "user-journey"), "product-doc"),
    "methodology": "methodology", "convention": "convention",
    "current-state": "current-state", "meeting-notes": "discussion",
    "discussion": "discussion", "voice-memo": "voice-memo",
    "build-report": "build-report", "runbook": "operational-runbook",
    "gotcha": "gotcha", "external": "source-document",
}
STATUS = {"draft", "accepted", "superseded", "deprecated"}
OUTCOMES = {"passed", "partial", "failed"}
ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z|[0-9a-f]{64}\Z")
REPORT_FIELDS = ("outcome", "assesses", "code")
MASK = "__hindsight_lifecycle_mask__"
MASKED = ("status", "updated_at")


def strict_json(text):
    """Parse JSON, rejecting duplicate keys. yq emits YAML duplicates verbatim."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate frontmatter field '{key}'")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def _report(doc):
    outcome, assesses, code = (doc.get(k) for k in REPORT_FIELDS)
    if outcome not in OUTCOMES:
        raise ValueError("build-report outcome must be passed, partial or failed")
    if not isinstance(assesses, list) or not assesses:
        raise ValueError("build-report assesses must list the exact document revisions it assessed")
    seen = set()
    for entry in assesses:
        if (not isinstance(entry, dict) or set(entry) != {"id", "fingerprint"}
                or not isinstance(entry["id"], str) or not ATOM.fullmatch(entry["id"])
                or not isinstance(entry["fingerprint"], str) or not HEX.fullmatch(entry["fingerprint"])):
            raise ValueError("each assesses entry needs exactly an id and a quoted 64-hex fingerprint")
        if entry["id"] in seen or entry["id"] == doc["id"]:
            raise ValueError("assesses entries must be unique and must not name the report itself")
        seen.add(entry["id"])
    if not isinstance(code, list) or not code:
        raise ValueError("build-report code must list each assessed repository commit")
    repos = set()
    for entry in code:
        if (not isinstance(entry, dict) or set(entry) != {"repo", "commit"}
                or not isinstance(entry["repo"], str) or not ATOM.fullmatch(entry["repo"])
                or not isinstance(entry["commit"], str) or not COMMIT.fullmatch(entry["commit"])):
            raise ValueError("each code entry needs exactly a repo and a quoted full commit SHA")
        if entry["repo"] in repos:
            raise ValueError("duplicate code repo entry")
        repos.add(entry["repo"])
    return dict(outcome=outcome, assesses=[dict(e) for e in assesses], code=[dict(e) for e in code])


def validate(doc):
    if not isinstance(doc, dict):
        raise ValueError("frontmatter must be an object")
    if not any(k in doc for k in ("schema_version", "id", "type")):
        return {"verdict": "skip"}
    for field in ("id", "type", "title", "status", "source", "scope", "updated_at"):
        if not isinstance(doc.get(field), str) or not doc[field].strip():
            raise ValueError(f"missing or non-string {field}")
    if "schema_version" in doc and (type(doc["schema_version"]) is not int or doc["schema_version"] != 2):
        raise ValueError("unsupported schema_version, expected 2")
    if not ATOM.fullmatch(doc["id"]):
        raise ValueError("id must contain only letters, digits, dots, underscores, or hyphens")
    kind = doc["type"]
    if kind not in STRATEGIES:
        raise ValueError(f"unknown type '{kind}'")
    for field, values in (("status", STATUS), ("scope", {"business", "platform", "repo"}), ("source", {"human", "agent", "external"})):
        if doc[field] not in values:
            raise ValueError(f"bad {field} '{doc[field]}'")
    for field in ("updated_at", "created_at"):
        if field not in doc:
            continue
        value = doc[field]
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
            raise ValueError(f"{field} must be an RFC 3339 timestamp with timezone")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid {field}") from None
    for field in ("repos", "domains"):
        values = doc.get(field, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and ATOM.fullmatch(v) for v in values):
            raise ValueError(f"{field} must be an array of nonempty tag values")
        if len(set(values)) != len(values):
            raise ValueError(f"duplicate {field} values")
    report = None
    if kind == "build-report":
        report = _report(doc)
    elif any(field in doc for field in REPORT_FIELDS):
        raise ValueError("outcome, assesses and code belong only to build-report documents")
    repos, domains = doc.get("repos", []), doc.get("domains", [])
    if doc["scope"] == "repo" and not repos:
        raise ValueError("scope repo requires at least one repo")
    known = os.environ.get("HINDSIGHT_KNOWN_REPOS", "").splitlines()
    for repo in repos:
        if known and repo not in known:
            print(f"WARN {doc['id']}: repo '{repo}' matches no rig", file=sys.stderr)
    vocab = os.environ.get("HINDSIGHT_KNOWN_DOMAINS_FILE", "")
    if vocab and Path(vocab).is_file():
        known_domains = Path(vocab).read_text().splitlines()
        for domain in domains:
            if domain not in known_domains:
                print(f"WARN {doc['id']}: new domain '{domain}'", file=sys.stderr)
    context = f"{kind}: {doc['title']}"
    if kind == "voice-memo":
        context += "; owner thinking, not a decision"
    if kind == "current-state":
        context += "; OBSERVED state, not a decision or precedent"
    if report:
        # Evidence of what was built, never approval of intent or deployment.
        context += (f"; IMPLEMENTATION EVIDENCE, build outcome {report['outcome'].upper()}; assesses "
                    + ", ".join(e["id"] for e in report["assesses"])
                    + "; not approval, deployment, or proof that every target requirement shipped")
    if domains:
        context += "; domain " + ", ".join(domains)
    if repos:
        context += "; repo " + ", ".join(repos)
    context += {
        "draft": "; DRAFT assessment, not human-reviewed" if report else "; DRAFT, not current platform direction",
        "accepted": "; ACCEPTED record; its document type determines what it establishes",
        "superseded": "; SUPERSEDED, retained as history, not current platform direction",
        "deprecated": "; DEPRECATED, no longer holds",
    }[doc["status"]]
    tags = ([f"scope:{doc['scope']}"] + [f"repo:{v}" for v in repos] + [f"domain:{v}" for v in domains]
            + [f"memory_type:{kind}", f"source:{doc['source']}", f"status:{doc['status']}"])
    scopes = [[f"domain:{v}"] for v in domains] + [[f"repo:{v}"] for v in repos] + [[f"scope:{doc['scope']}"]]
    lifecycle = dict(status=doc["status"], type=kind)
    if report:
        lifecycle.update(report)
    return dict(verdict="ship", document_id=doc["id"], strategy=STRATEGIES[kind], tags=tags,
                observation_scopes=scopes, context=context, timestamp=doc["updated_at"],
                lifecycle=lifecycle)


def _yq(text):
    try:
        result = subprocess.run(["yq", "--front-matter=extract", "-o=json", "."], input=text,
                                capture_output=True, text=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("cannot parse masked frontmatter") from None
    return strict_json(result.stdout)


def fingerprint(text, parsed, parse=_yq):
    """Hash the file with only the status and updated_at values masked.

    Each masked field must be exactly one plain top-level ``key: value`` line in
    the frontmatter. The masked text is parsed again and must equal the original
    frontmatter with only those two values replaced, so a block scalar, a quoted
    or flow-mapped duplicate, or a boundary disagreement cannot widen the mask.
    """
    lines = text.splitlines(keepends=True)
    if not lines or not re.fullmatch(r"---[ \t]*\r?\n?", lines[0]):
        raise ValueError("fingerprint requires YAML frontmatter")
    end = next((i for i in range(1, len(lines)) if re.fullmatch(r"---[ \t]*\r?\n?", lines[i])), None)
    if end is None:
        raise ValueError("unterminated frontmatter")
    found = {key: [] for key in MASKED}
    for index in range(1, end):
        match = re.match(r"(status|updated_at)[ \t]*:", lines[index])
        if match:
            found[match.group(1)].append(index)
    for key, indexes in found.items():
        if len(indexes) != 1:
            raise ValueError(f"frontmatter needs exactly one plain top-level {key} line")
        index = indexes[0]
        match = re.fullmatch(r"(" + key + r"[ \t]*:[ \t]+)(\"[^\"\n]*\"|'[^'\n]*'|[^\s#'\"][^\s#]*)"
                             r"((?:[ \t]+#[^\r\n]*)?[ \t]*\r?\n?)", lines[index])
        if not match:
            raise ValueError(f"{key} must be a single-line scalar")
        lines[index] = match.group(1) + MASK + match.group(3)
    masked = "".join(lines)
    expected = dict(parsed, **{key: MASK for key in MASKED})
    if parse(masked) != expected:
        raise ValueError("status and updated_at must be plain single-line top-level frontmatter values")
    return hashlib.sha256(masked.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    doc = {}
    try:
        doc = strict_json(sys.stdin.read())
        result = validate(doc)
        if len(sys.argv) == 3 and sys.argv[1] == "--file" and result["verdict"] == "ship":
            text = Path(sys.argv[2]).read_bytes().decode("utf-8")
            result["lifecycle"]["fingerprint"] = fingerprint(text, doc)
    except (ValueError, OSError, UnicodeError) as error:
        result = dict(verdict="refuse", document_id=doc.get("id", "?") if isinstance(doc, dict) else "?", reason=str(error))
    print(json.dumps(result))
