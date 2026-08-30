---
schema_version: 2
id: adr.storage.report-format.0002
type: adr
title: Report Storage Format v2 — Structured JSON
status: accepted
source: human
scope: platform
repos:
  - svc-reports
domains:
  - storage
created_at: 2026-08-12T00:00:00Z
updated_at: 2026-08-15T09:00:00Z
---
# Report Storage Format v2 — Structured JSON

## Decision

Generated reports are stored as **structured JSON documents** in S3 under
`s3://reports/{company}/{report_id}.json`, schema version `report.v2`.
Rendered HTML is produced at publish time and stored beside the JSON as
`{report_id}.html` — never rendered on read.

This **supersedes `adr.storage.report-format.0001`** (Markdown-in-S3,
render-on-read).

## Requirements

- The JSON document is the source of truth; the HTML artifact is derived
  and reproducible from it.
- Every numeric metric in the JSON carries its value, unit, and a
  `source_ref` pointing at the upstream fact it was computed from.
- Schema changes bump `report.v2` → `report.v3` etc.; readers reject
  documents with a schema version they do not know.

## Alternatives considered

- **Keep Markdown, add a sidecar metrics file**: rejected — two artifacts
  that can disagree.
- **HTML as source of truth**: rejected — presentation format, not data.

## Consequences

- The founder review surface is the rendered HTML, not the stored JSON.
- Report regeneration replaces both artifacts atomically.
