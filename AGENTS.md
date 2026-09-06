# Review Pitfalls

- Existing uncommitted files include protected reference documents and survey
  work. Do not edit, remove, stage, or commit them without the user's approval.
- The old 42-test suite and `gc lint .` passed despite broken prompt paths.
  Gas City's `buildTemplateData` in `cmd/gc/prompt.go` supplies no `ConfigDir`;
  formula `{{.ConfigDir}}` references are also not ordinary formula variables.
  Use pack commands, and test rendered instructions plus real command dispatch.
- A stored `document_metadata.content_hash` is not proof of successful retain.
  Hindsight's streaming pipeline commits document metadata with its first
  batch, before later batches finish. Verified in local v0.9.1 and v0.9.2
  source. The old shipper skipped such failures and reported healthy scans.
  Completion receipts now live in Beads; tests must keep modeling partial commits.
- The old shell walker masked `git ls-tree` failures and ignored filesystem
  process-substitution exit statuses. The Git-only snapshot reader must observe
  every traversal/read failure. Missing local docs directories do not establish
  absence from the published tree; missing rig checkouts must not silently fall
  back to a parent city repository.
- Mental-model `trigger.tag_groups` filters refresh inputs, not just scheduling,
  in the local Hindsight v0.9.2 source. A positive `status:accepted` requirement
  excludes this pack's observations because their scopes omit status. It does
  not establish human approval or make an accepted voice memo a decision.
- A custom Beads type does not automatically stay out of ready work. Durable
  ingestion records should not be ordinary open tasks. Beads supports the
  persistent `pinned` status, excluded by default ready status selection;
  verify Gas City work queries too. Keep ingestion outcome in metadata, separate
  from bead lifecycle, and use the explicit city store rather than cwd routing.
- Beads `status: pinned`, its separate boolean `pinned` field, and Gas City's
  `gc session pin` are different mechanisms. Use the status for durable document
  records and query `--status pinned`, not `--pinned` (the boolean selector).
  Metadata-only updates remain allowed without force. Normal compaction requires
  closed status; normal close rejects pinned records unless forced. Pinned
  dependency targets count as satisfied in Beads, so ingestion records must not
  act as blocking prerequisites. Keep records unassigned and unrouted. Gas Town's
  `internal/beads/handoff.go` provides prior art for mutable, status-pinned
  records; this does not imply pinning provides locking or immutability.
- Current Gas City imports packs through `[imports.<binding>]`, not the old
  `[workspace.pack] path` syntax. Test discovered commands from a fixture city,
  without `gc init` (which would register/start it). In the installed gc, a
  pre-binding `--city` can be forwarded into pack script arguments; use the
  documented city-relative `gc hindsight ...` invocation. The internal Beads
  adapter separately uses `gc bd --city` to force the city store.
- Hindsight's document reprocess endpoint has no caller-generated operation ID.
  A lost acknowledgement must leave `reprocess_prepared` unresolved, not cause
  a blind retry. Normal retain supports idempotent UUID submissions. Batch parent
  operation summaries omit children's extraction error counts; fetch each child's
  full status before recording a successful receipt.
- The offline suite makes no LLM calls; `test/evaluate.py` is a separate opt-in
  live evaluation. A 2026-09-05 profile on this Mac measured 157.7 seconds for
  183 tests: 124 seconds in `test_pack.py`, versus 0.026 seconds for the 67
  in-process ingestion tests. The subprocess suites logged about 2,500 mock
  command launches, each starting Python; PATH also routes `python3` through
  mise. Prefer in-process coverage for state/validation permutations and a
  smaller set of actual CLI dispatch tests rather than blaming Python execution
  speed or assuming the suite is waiting for extraction.
- Beads metadata is inline JSON as of `db2a79e`, not an `@file` argument.
  The adapter tests initially retained the old file contract and failed despite
  the production fix. Keep fixtures aligned with the actual CLI contract;
  `BeadsStore.put` owns write confirmation, not its callers.
- Keep `test/__init__.py`: without it, targeted `test.test_*` invocations can
  resolve Python's installed `test` package instead of this suite. Discovery
  with `-s test` can still pass while the targeted pre-push command fails.
