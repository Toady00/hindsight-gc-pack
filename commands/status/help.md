# status

Show the latest run, last full reconciliation, coverage and counts, last
successful full-scan timestamp, freshness and unresolved document attempts as
JSON. This command reads shared city Beads records; it does not call the live
bank or wake an agent.

`gc hindsight status [--bank <id>] [--api <url>]`

Exit 0 requires a successful full scan within `HINDSIGHT_MAX_SHIP_AGE` seconds,
default 7200, clean `latest_full_scan` and `latest_run`, and no unresolved
document attempts. Exit 1 means one of those conditions is unmet. Partial scans
appear under `latest_run` and never reset the full-scan success timestamp;
an incomplete partial scan still makes health unhealthy. `unresolved_documents`
lists the document IDs needing recovery or inspection. Missing health history
is unknown/unhealthy, not an empty successful scan. Invalid or unavailable
Beads state returns an error, normally exit 5; configuration validation can also
return exit 2.

`GC_CITY_PATH` must identify the absolute city path configured for the shared
city SQL database. Reads use `gc bd --city "$GC_CITY_PATH"`, not cwd-based rig
routing. The `hindsight-bank` record holds health in `metadata.hindsight.data`;
`hindsight-document` records hold ingestion attempts and successful receipts.
Both use durable lifecycle status `pinned`, unassigned and unrouted, with
`metadata.hindsight.schema_version: 1` and endpoint/bank/kind/document identity.

There is no `HINDSIGHT_STATE_DIR` health journal or local report fallback.
Another machine needs the same city Beads store, not files from the archivist's
host. The store is also the shipping ledger: losing receipts means bank hashes
cannot be trusted for skipping. See [operations](../../OPERATIONS.md) for
reviewed bootstrap and recovery, including fail-closed reprocess repair.
