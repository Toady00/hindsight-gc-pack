# Operating the memory pack

The default docs schema now requires `status` on every document. `type` still
selects extraction strategy. `status` records draft, accepted, superseded, or
deprecated. These are separate decisions. Accepting a survey records its
standing as an observation; it does not make its contents a platform decision.
Accepting a voice memo does not turn thinking into an adopted plan.

Required fields are `id`, `type`, `title`, `status`, `source`, `scope`, and
`updated_at`. Timestamps must be valid RFC 3339 values with a timezone. Repos
and domains must be arrays of nonempty tag values. Repo-scoped documents need
at least one repo. If present, `schema_version` must be 2. A document with
schema markers but missing required fields is refused, not silently skipped.
The raw `null` dialect and custom schema verdicts must also carry exactly one
valid `status:` tag; their remaining vocabulary stays operator-owned.

Existing documents without status need a deliberate source edit. Choose the
status; do not assign `accepted` in bulk to get past validation. This also
applies to survey producers: older producers that omit `status` need an
explicit update. Do not bypass validation or infer acceptance. No automatic
migration changes documents or the bank. Existing bank-native memories without
status require `gc hindsight retain --bump --id <id> --status <chosen-status>`.
New arbitrated memories default to accepted because the archivist has accepted
the proposal. Type and source continue to qualify what that acceptance means.

## Shipping and connection settings

`gc hindsight ship` submits work to the archivist. It does not wait for shipping
when called by a human or another agent. Within the managed archivist it runs
in the foreground. Follow the returned formula bead for queued work's
completion. `gc hindsight ship --dry-run <root>` previews locally. See
[ship command help](commands/ship/help.md) for flags and outcomes.

Use `gc hindsight read`, `gc hindsight ship`, `gc hindsight maintain`,
`gc hindsight retain`, and `gc hindsight status` for the pack's memory commands.

The archivist runs one write script at a time in the foreground. Its agent
configuration supplies `HINDSIGHT_WRITER=archivist`; the scripts also require a
managed session ID. This is an accidental-bypass guard, not credential-based
access control. Do not export the marker in workspace-wide env or give it to
other agents. A city-local advisory lock prevents overlapping ship processes,
including surviving tools from an old session. It is not a distributed lock
and does not serialize arbitrary external clients. Keep exactly one active writer
per bank;
multiple cities writing the same bank are unsupported.

Endpoint precedence is explicit `--api`, then `HINDSIGHT_API`, then
`HINDSIGHT_API_URL`, then the selected CLI profile or config file. No localhost
or other server default is supplied. A missing selected profile/config fails
when connection resolution needs that file.
`HINDSIGHT_PROFILE` selects `~/.hindsight/cli-profiles/<name>.toml`;
`HINDSIGHT_CONFIG` can select an explicit config file when no profile is set.

Credentials come from `HINDSIGHT_API_KEY`, or from the selected config when
its endpoint is used. An explicit endpoint never borrows credentials from a
config for another endpoint. Both HTTP and CLI requests use the resulting
endpoint and credential. HTTP auth travels over stdin to curl. Task requests
and health reports contain no credentials. For standalone read calls, use
`gc hindsight read` with the ordinary CLI read arguments.

Runtime dependencies are Bash, Git, curl with `--fail-with-body`, jq, Mike
Farah's yq, Python 3.11 or newer, Hindsight CLI, and Gas City. Python's standard
library handles connection files, validation, request encoding and reports.

## Archivist workflow dispatch

The archivist uses `wake_mode = "resume"`. In gc `4eb766c0b`, fresh mode can
cycle a live session on the next controller tick after it claims another bead,
even if that bead's foreground shipping command has begun. Resume mode avoids
that deliberate claim-transition reset; explicit operator resets remain possible.
After closing a root, the worker checks the hook again before going idle.

Shipping attributes each attempt and confirmed receipt to the canonical claim
on the session bead. `gc hindsight status --task <root-id>` returns those receipt
summaries plus recent scan snapshots. A reset followed by a zero-write drain
timeout therefore leaves earlier successes visible. Bank health is still
reported independently, and an interrupted scan is not promoted to success.
An ordinary retry skips matching confirmed receipts. Re-entering the same
task's force request also skips an already confirmed matching reprocess; a
separate task's explicit force request still runs.

Shipping and maintenance use root-only vapor tasks. Their root descriptions
contain the entire ordered workflow, including failure handling, report notes,
and root closure. Shipping runs sync then reporting. Maintenance runs the script,
audits mental models only after exit 0 or 2, then reports even if earlier work
was incomplete. There are no child beads to claim or resume independently.

With gc `4eb766c0b`, `pour = true` creates legacy molecule roots and step children
that controller Ready/demand intentionally excludes. `bd ready` displaying a
child is not evidence that an on-demand worker will wake. Do not remove those
upstream exclusions, and do not remove `pour` while leaving work only in child
descriptions. In a new city, template-routed work can wake an ephemeral demand
worker; an existing named identity can instead resume. Keep the archivist's capacity at
one. Close work as its confirmed assignee rather than forcing ownership.

### Recovering already-stranded runs

Formula edits do not retrofit existing beads. Before changing live runs, inspect
their root and all children, assignments, notes, outstanding bank operations,
and document `attempt` / `last_success` receipts. Agree on the recovery plan with
the operator. Never infer failed extraction from a bank-drain timeout alone.

The September 22 read-only inspection in `las-vegas` found scheduled roots
`lv-mt9e` (shipping) and `lv-c5ga` (maintenance) still open and unassigned, with
legacy children. The archivist was asleep. All five September 19 ingestion
reprocess receipts remained successful, no document attempts were unresolved,
and the bank reported zero pending or processing operations. The health record
still reflected the September 19 incomplete scan; it did not negate the receipts.

Recovery procedure, only after approval and another state check:

1. Record the old roots, child states, and receipt evidence in their notes.
2. Retire the unstarted legacy children and roots as superseded by the corrected
   workflow, not as executed successfully. Keep their history; do not delete or
   rewrite document receipts. If anything is now claimed, stop and coordinate
   with its owner instead of forcing closure.
3. Let the scheduler create one replacement shipping run and one maintenance
   run, or dispatch them deliberately while avoiding duplicate scheduled work.
   Open old roots suppress replacement order runs.
4. Use ordinary shipping, not `--reprocess`: confirmed extraction does not need
   repeating. A normal scan will still ingest any genuinely changed published
   documents. Verify demand, claim, notes, root closure, and bank health.

The unsplit-city route-recovery warning about storage migration is separate;
storage migration is not part of this repair.

This recovery was approved and performed on September 23 UTC. Legacy children
were retired as superseded, and Beads auto-closed their roots. Recovery notes,
labels, and metadata distinguish that retirement from successful execution.
Replacement maintenance `lv-dq60` completed cleanly. The initial replacement
shipping run `lv-f93v` encountered the session-continuation and reporting issues
addressed above. By September 24 at 00:50:24 UTC, ordinary scan `lv-amu7` had
completed successfully with all 11 published documents unchanged, zero failures,
and healthy bank status. The four most recent shipping roots were closed with
reports, and there were no unresolved document attempts.

Before retaining documents, the shipper checks `/openapi.json`. The server's
`RetainRequest` schema must advertise client-supplied `operation_id` and boolean
`async`; otherwise writes are refused. The templates were built against local
Hindsight v0.9.1/v0.9.2 servers that support this contract. Check the deployed
server rather than assuming its version or template import proves support.

## Git publication

Every scan, including `--dry-run`, freshly fetches `origin`. The default branch
comes from the remote's advertised HEAD, not local `origin/HEAD`. `--ref` selects
a published branch and accepts `foo`, `origin/foo`, `refs/heads/foo`, or
`refs/remotes/origin/foo`. Local HEAD, commit SHAs, tags and local-only branches
are not supported. `--fetch` remains accepted for existing callers but is a
no-op: fetching is mandatory even without it.

Each root must belong to a Git repository, including a Git worktree. There is
no filesystem fallback. Automatic discovery includes every registered rig's
`docs/`, even when that directory is absent from its current checkout, plus
`HINDSIGHT_DOCS_ROOTS`. City-root `docs/` is also scanned when the city belongs
to Git, even if absent in the checkout. An existing non-Git city docs directory
is refused. A missing rig repository is an error. A docs path
absent from the fetched tree is a valid empty inventory, including for GONE.

Push to the canonical branch to publish. A pushed `status: draft` document is
visible; status is not a publication gate. Dirty files, untracked files, local
commits and the checked-out branch do not affect the shipped content. A selected
`--ref` branch is also a publication source, so do not use it for private drafts.
No accepted-source filters are added.

The shipper pins each repository's fetched commit while reading its trees and
blobs. Fetch, tree-listing or blob-read failure in any root stops the scan before
any new retain, including recovery submissions. It never ships stale cached
content after a failed fetch. Docs-source metadata records `kind: git`, stable
origin-based `repository` identity, repository-relative `relpath`, `ref` and
commit SHA. Bank document metadata carries the corresponding `repository`,
`relpath`, `source_ref` and `source_commit`. These Git source records cover the
docs/code corpus, not bank-native memories or session transcripts.

## Durable ingestion

The city Beads store is the shipping ledger. Commands address it explicitly as
`gc bd --city "$GC_CITY_PATH"`; `GC_CITY_PATH` must be an absolute city path
configured for the shared city SQL database. They do not route through the
current rig's store. A machine handoff needs access to that same city store and
the source repositories, not a copied local health journal.

Document records use custom type `hindsight-document`; the bank health record
uses `hindsight-bank`. Both are ordinary durable Beads records with lifecycle
status `pinned`, unassigned and unrouted. They are not ephemeral tasks, and
ingestion success does not close them. This is the `pinned` status, not the
separate pinned boolean or a pinned Gas City session. Normal metadata updates
need no force. Pinning is not a lock; do not assign, route, force-close or use
these records as blocking prerequisites.

`metadata.hindsight` contains `schema_version: 1` and the identity fields `api`,
`bank`, `kind`, `document_id`. The bank record has an empty `document_id`.
`metadata.hindsight.data` holds a document's `attempt`, `last_success` and
`source`, or a bank's `latest_run`, `latest_full_scan` and `last_success`.
Unrelated top-level metadata is preserved. Invalid or duplicate records require
repair; they are not permission to start a fresh attempt.

Before POSTing retain, the shipper persists and reads back the operation intent,
including a caller-generated UUID and the full request payload snapshot. It
records success only after terminal completion without reported extraction
errors. Failed or interrupted attempts remain unresolved. After draining active
bank operations, the next run recovers the original operation before starting
new work, even if the source moved or disappeared. Recovery polls the saved ID,
replays the saved retain request once if not found, or retries a failed/cancelled
operation at most once per recovery. Timeouts leave the intent for a later run;
inconsistent state and extraction-error completions require inspection.

For bank-native memories, retrying `gc hindsight retain` first recovers any
unfinished attempt for that ID. If recovery succeeds, it reports `RECOVERED`
and exits without counting another hit or consuming a new proposal. Run it
again only when submitting a separate report.

The pending payload stays in Beads until successful completion, then is removed
from current metadata. Dolt history retains earlier values, including document
content. Treat the city database and its history as copies of the source corpus.

A bank `document_metadata.content_hash` alone does not prove completion: a failed
streaming retain can already have stamped it. Skipping requires a matching
successful Beads receipt for both source and derived payload, plus the bank
hash. If the ledger is missing or success is unknown, do not seed receipts from
bank hashes. Review `gc hindsight ship --dry-run`, then run a real scan. Existing
documents re-ingest once to establish successful receipts. Dry-run reads the
bank and Beads but neither retains nor updates health.

## Health and maintenance

`gc hindsight status` reads shared Beads records and prints JSON without calling
the live bank or waking an agent. It shows `latest_run`, `latest_full_scan`,
`last_success_at`, age and `unresolved_documents`. Exit 1 means no successful
full scan, stale success, a latest run or full scan that is not clean, or an
unresolved document attempt. `HINDSIGHT_MAX_SHIP_AGE` defaults to 7200 seconds.
A successful partial scan cannot refresh the full-scan success timestamp.

The `hindsight-bank` record stores roots, fetched commit SHAs, findings, counts
and timestamps. An interrupted run may remain `running`; it never manufactures
a successful result. There is no `HINDSIGHT_STATE_DIR` health journal or local
report-directory fallback. Monitoring must reach the same explicit city Beads
store, even from another machine. Unavailable or invalid Beads state is an error,
not healthy status.

Failed operation checks stop shipping and consolidation. Maintenance paginates
all tags and uses the same rig-registry response shape as root discovery.
Unreachable rig-registry checks report incomplete audits. GONE considers only
the fetched repository/path prefixes, so a partial-root scan does not accuse
another docs tree of losing its files. GONE never deletes bank content.

After changing extraction settings or derivation rules, use a reviewed
`gc hindsight ship --reprocess <roots>` request, previewed with `--dry-run`.
This first retains current source and parameters, waits for completion, then
calls the server's document reprocess endpoint and waits again. It can incur
extraction costs for every selected document. It requests explicit reprocessing;
it is not a verified guarantee that a server invalidates every unchanged chunk
or applies every extraction-setting change.

The reprocess endpoint does not accept a caller idempotency ID. Before calling
it, the shipper persists `metadata.hindsight.data.attempt.phase` as
`reprocess_prepared`. If the acknowledgement is lost, subsequent runs fail
closed. Never blindly rerun the POST or clear the attempt. A human must inspect
the bank's operation list, identify the actual reprocess operation, and repair
`metadata.hindsight.data.attempt.reprocess_operation_id` with that UUID and
`metadata.hindsight.data.attempt.phase` with `reprocess_wait`, preserving the
rest of the record. Then an ordinary scan can resume polling/recovery. If no
operation can be identified, leave it blocked for investigation. A known failed
operation follows the bounded retry path; it is not a new reprocess request.

## Verification

Run offline checks with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s test -p 'test_*.py' -v
gc lint .
```

The tests invoke production scripts with temporary Git repositories and mocked
Hindsight, curl and gc processes. They never write to a live bank. See
[test/README.md](test/README.md) for the separate, optional live evaluation.

The opt-in [archivist runtime regression](test/ARCHIVIST_RUNTIME.md) uses the
installed controller, subprocess sessions, and an isolated Beads/Dolt database:

```bash
HINDSIGHT_RUNTIME_TEST=1 python3 -B -m unittest test.test_archivist_runtime -v
```

It verifies instantiated eligibility, controller demand, on-demand wake, hook
claim, ordered execution through pack wrappers, report persistence, and root
closure for both workflows. Hindsight and the model provider are mocked; no live
city or bank is used. A legacy-pour negative control checks that `bd ready` alone
does not imply controller eligibility.
