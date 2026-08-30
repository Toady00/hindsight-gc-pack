---
schema_version: 2
id: adr.storage.report-format.0001
type: adr
title: Report Storage Format
status: superseded
source: human
scope: platform
repos:
  - svc-reports
domains:
  - storage
created_at: 2026-07-10T00:00:00Z
updated_at: 2026-08-15T09:00:00Z
---
# Report Storage Format

**Status: SUPERSEDED by `adr.storage.report-format.0002` (2026-08-15).
This is retained as history; it does not describe the current platform.**

## Decision (superseded)

Generated reports are stored as **rendered Markdown files** in S3 under
`s3://reports/{company}/{report_id}.md`. Markdown is the single storage
format; HTML is rendered on read.

## Rationale (at the time)

- Markdown is directly reviewable by the founder without tooling.
- One artifact per report keeps the storage model trivial.

## Why it was superseded

Rendering on read coupled presentation to storage, and structured data
(metrics tables) had to be parsed back out of prose for the API. See
`adr.storage.report-format.0002` for the replacement.
