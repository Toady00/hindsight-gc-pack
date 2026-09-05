#!/usr/bin/env python3
"""The docs contract, shared by document and bank-native writers."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
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
ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


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
    if domains:
        context += "; domain " + ", ".join(domains)
    if repos:
        context += "; repo " + ", ".join(repos)
    context += {
        "draft": "; DRAFT, not current platform direction",
        "accepted": "; ACCEPTED record; its document type determines what it establishes",
        "superseded": "; SUPERSEDED, retained as history, not current platform direction",
        "deprecated": "; DEPRECATED, no longer holds",
    }[doc["status"]]
    tags = ([f"scope:{doc['scope']}"] + [f"repo:{v}" for v in repos] + [f"domain:{v}" for v in domains]
            + [f"memory_type:{kind}", f"source:{doc['source']}", f"status:{doc['status']}"])
    scopes = [[f"domain:{v}"] for v in domains] + [[f"repo:{v}"] for v in repos] + [[f"scope:{doc['scope']}"]]
    return dict(verdict="ship", document_id=doc["id"], strategy=STRATEGIES[kind], tags=tags,
                observation_scopes=scopes, context=context, timestamp=doc["updated_at"])


if __name__ == "__main__":
    doc = {}
    try:
        doc = json.load(sys.stdin)
        result = validate(doc)
    except (ValueError, OSError) as error:
        result = dict(verdict="refuse", document_id=doc.get("id", "?") if isinstance(doc, dict) else "?", reason=str(error))
    print(json.dumps(result))
