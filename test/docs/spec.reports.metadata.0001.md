---
schema_version: 2
id: spec.reports.metadata.0001
type: spec
title: Report metadata persistence
status: accepted
source: human
scope: repo
repos:
  - svc-reports
updated_at: 2026-08-05T12:00:00Z
---
# Report metadata persistence

svc-reports persists report metadata in its own service datastore. This metadata
is separate from the report artifacts governed by the report-format ADR.
