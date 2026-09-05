# ship

Queue a document reconciliation for the city's single archivist.
Within the managed archivist, the same command executes the reconciliation.

```text
gc hindsight ship                    Queue a full scan using the archivist's roots and bank
gc hindsight ship <root> [<root>...]  Queue a partial scan; relative paths resolve at submission
gc hindsight ship --dry-run <root>   Preview locally without retaining or updating health
gc hindsight ship --fetch            Accepted no-op; every scan freshly fetches origin
gc hindsight ship --reprocess        Retain, then request server reprocessing even for unchanged docs
gc hindsight ship --bank <id>        Select a bank; otherwise use HINDSIGHT_BANK
```

Also accepts `--api`, `--ref`, `--domains`, `--schema`, and
`--drain-timeout`. `--schema` defaults to `HINDSIGHT_SCHEMA` or the pack's
`schemas/docs`. All roots in a run use that schema.

Outside the archivist, exit 0 means queued, not shipped. The dispatched
formula bead carries completion and findings. `HINDSIGHT_ARCHIVIST` overrides
the target; the default is `<pack binding>.archivist`. The command transports
arguments as encoded JSON, never shell source. Do not put credentials in
arguments or URLs. The archivist must have credentials and access to requested
paths. Caller-local CLI credentials are never copied into the task.

Only the managed archivist receives `HINDSIGHT_WRITER=archivist` and may call
write scripts. Run scripts in the foreground, sequentially. A read-only preview
needs its own bank, connection settings and access to the city Beads store.
One city owns each writable bank; there is no distributed lock or protection
against a separate city writing it.

Every scan freshly fetches `origin`, including previews. The remote's advertised
HEAD selects the default branch; local `origin/HEAD` and local HEAD are not
fallbacks. `--ref` selects a published remote branch and accepts `foo`,
`origin/foo`, `refs/heads/foo` and `refs/remotes/origin/foo`. Commit SHAs, tags,
HEAD and local-only branches are unsupported. Push to the canonical branch to
publish, including drafts. Worktree changes and unpushed commits are irrelevant;
there is no accepted-source filter or filesystem fallback.

Automatic roots include every rig's `docs/` from the city registry, even when
absent from its checkout, plus `HINDSIGHT_DOCS_ROOTS`. City-root `docs/` is
also scanned when the city belongs to Git, even if absent locally. Every root must belong to Git, including worktrees
and city docs. A missing rig repository fails; a docs path absent from the
fetched tree is a valid empty inventory. Fetch, tree-listing or blob-read failure
in any root means no new retains for the entire scan. Git source records carry
stable origin-based repository identity, relative path, branch and commit SHA.
GONE is restricted to fetched repository/path prefixes and never deletes content.

Shipping uses durable `hindsight-document` records and a `hindsight-bank` health
record in the shared city SQL database, explicitly addressed through
`GC_CITY_PATH`. Records remain status `pinned`, unassigned and unrouted.
`metadata.hindsight` has schema version 1, endpoint/bank/kind/document identity,
and `.data` with attempts/receipts or bank health. No local health journal or
`HINDSIGHT_STATE_DIR` is used. `gc hindsight status` reads these records without
calling the bank and reports freshness and unresolved attempts. Explicit roots
are partial scans and cannot refresh the full-scan success timestamp.

The shipper persists a UUID operation intent and full payload snapshot in Beads
before retain POST, then records success only after confirmed completion. The
next run recovers the original operation, with bounded retry, before new work.
The pending payload is removed from current metadata on success; Dolt history
still retains earlier values. A bank hash alone is not a success receipt. For a
missing/unknown ledger, review `gc hindsight ship --dry-run`, then run a real
scan to re-ingest once and establish receipts. Invalid records require repair.
The server's `/openapi.json` must advertise `RetainRequest.operation_id` and
boolean `async`; local v0.9.1/v0.9.2 servers used for the templates support them.

Real execution returns 1 for refused docs or failed extraction, 2 for admission,
argument or capability errors, 3 for drain/active-operation timeout, and 5 for
unavailable state, unknown outcomes or incomplete coverage. Connection-resolution
errors return 2. GONE is report-only but prevents
the run from being recorded as a successful full scan.

`--reprocess` can incur extraction costs for every selected document. Preview
it first. It retains current source and parameters, waits, calls the server's
document reprocess endpoint and waits again. This requests reprocessing; it is
not a verified guarantee of server-side unchanged-chunk invalidation.

That endpoint is not caller-idempotent. The shipper records phase
`reprocess_prepared` before calling it. A lost acknowledgement fails closed:
never blindly retry the POST or clear the attempt. A human must identify the
actual operation in the bank's operation list and repair
`metadata.hindsight.data.attempt.reprocess_operation_id` with its UUID and
`metadata.hindsight.data.attempt.phase` to `reprocess_wait`, preserving other
metadata. An ordinary scan can then recover it. If the operation cannot be
identified, keep the attempt blocked. See [operations](../../OPERATIONS.md).
Existing bank-native memories are outside this Git docs walk.
