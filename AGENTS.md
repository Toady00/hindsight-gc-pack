# Review Pitfalls

- In installed gc `4eb766c0b`, `cycleAliveSessionForFreshReassign` kills an
  already-live `wake_mode=fresh` session when its assigned work diverges from
  `currently_processing_bead_id`. The September 23 shipping interruption
  followed that fresh-reassignment path. The archivist now uses `resume` for
  sequential queue consumption. Receipt attribution uses the canonical
  `current_claim_bead_id`, not the lagging controller anchor. A city-local
  shipping lock prevents a reset's surviving ship process from overlapping
  another ship process. New attempts/receipts/scans carry work_id; old receipts
  cannot be retroactively attributed. Status --task separates receipt evidence
  from the last scan's counters, with only 20 prior scan snapshots retained.

- During approved live recovery on 2026-09-23 UTC, retiring the last legacy
  children auto-closed `lv-mt9e` and `lv-c5ga` with generic `all steps complete`
  reasons. Explicit recovery notes/labels/metadata identify them as superseded
  without execution; do not interpret the auto-close reason as script success.
  The scheduler created root-only tasks `lv-dq60` and `lv-f93v`, and the existing
  named archivist woke and claimed work. This differs from the isolated fixture's
  ephemeral worker: do not generalize that fixture identity to an existing city.
  Maintenance completed cleanly. Shipping needed a continuation reminder after
  maintenance; the controller also performed a fresh continuation reset during
  shipping, and later scans waited on consolidation after new ordinary retains
  had succeeded. The final report covered the last timed-out scan, not all earlier
  writes. Inspect receipts and session continuity before attributing retries to
  model behavior or claiming unattended queue draining has been verified.

- Installed gc `1.4.2+local.4eb766c0b` rejects temporary HOME in `gc register`
  because it invokes platform supervisor start, even with a foreground supervisor
  already running. Isolated lifecycle tests should run `gc supervisor run` and
  seed only their temporary GC_HOME registry, not restore the real HOME.
- Installed gc's file store shares Ready candidate filtering with BdStore, but
  `gc hook --claim` still uses BdStore/`bd` for work claims. Use real isolated
  Beads for full lifecycle coverage. The file-store API also lacks Beads notes.
  The runtime test uses a temporary loopback Dolt server, configures it through
  `gc beads city use-external`, and registers Gas City's custom issue types.
- gc prepends its executable directory to a worker's PATH. A mock Hindsight
  CLI can be shadowed by the real CLI in that directory. Restore the fixture's
  allowlisted PATH inside the worker before executing any description commands.
- In new isolated cities on gc `4eb766c0b`, template-routed work wakes an ephemeral
  demand worker; an existing named identity can instead resume, as in live recovery.
  Ephemeral hook claims use the session bead ID,
  while the default close actor can be the pool runtime name. Close as the
  confirmed assignee with `--actor`, not `--force`. Immediate API bead reads
  can be stale after CLI claims; lifecycle assertions use `gc bd show` readback.
  A 200ms test patrol also exposed phantom pool identity collapse and worker
  reaping during claims; the fixture uses a 5s patrol, not accelerated polling.
- Scheduled empty-root shipping needs modern Bash: macOS Bash 3.2 rejects an
  empty `ROOTS[@]` expansion under `set -u`. The runtime fixture checks for and
  uses Bash 4.4+ rather than letting /bin/bash shadow the deployment shell.

- On 2026-09-19, `lv-dblx` was closed as blocked after repeated drain timeouts,
  but all five selected ingestion documents had successful reprocess receipts
  and completed child operations with zero extraction errors. `API.drain()`
  waits for all pending/processing bank operations, including consolidation and
  mental-model refreshes. Its generic "durable attempt retained" timeout does
  not establish a stuck document attempt. Check per-document receipts and live
  operation details before repeating `--reprocess`; later zero-shipped timeout
  scans do not negate earlier completed work. Consolidation completed at
  03:55:29 UTC, followed by mental-model refreshes; bank health still reflected
  the latest incomplete scan.

- In gc `4eb766c0b`, legacy poured molecule roots and their step children are
  intentionally excluded from controller Ready/demand. `bd ready` alone is not
  evidence they can wake the on-demand archivist. The `pour = true` fix in
  `2f6a28c` stranded roots `lv-mt9e` and `lv-c5ga`; the earlier successful
  `lv-dblx` was already claimed before that change and did not validate it.
  Ship and maintenance now use root-only vapor tasks with every ordered phase,
  reporting and root-close instructions in the root description, no children.
  Never remove `pour` while leaving executable instructions only in `[[steps]]`.
  Test actual instantiation, demand, wake, claim, execution and completion with
  `HINDSIGHT_RUNTIME_TEST=1`, not only `gc formula show`. Existing beads are not
  retrofitted; recheck live state and receipts and agree on recovery before edits.
  The unsplit-city route-recovery storage warning is unrelated to eligibility.

- On 2026-09-18, all 11 live `stacked-chips-v2` document records lacked
  `retain_params.strategy`, and the bank had no `retain_default_strategy`.
  Upstream v0.10.0 document reprocess replays stored params and forces extraction,
  but cannot recover an absent original strategy; UI reprocess would therefore
  use generic bank settings. Prefer reviewed `gc hindsight ship --reprocess`
  for Git-managed docs when restoring document-type parameters matters. It
  reads current published Git content, not necessarily the previously retained
  revision, and excludes bank-native records. Preview before running.

- Upstream v0.10.0 startup calls `load_dotenv_for_entrypoint()`, which uses
  `load_dotenv(find_dotenv(usecwd=True), override=True)`: a discovered `.env`
  can override container/process environment values. Configuration is cached
  per process. Set `HINDSIGHT_API_LLM_OUTPUT_LANGUAGE=English` on API and any
  separate workers and restart those processes; check for conflicting `.env`
  values. An isolated check against the actual config loader and consolidation
  prompt builder confirmed this variable resolves to English and replaces the
  source-language rule with the forced-English instruction. Bank config GET
  excludes this static field, so its absence there does not show it is unset.

- Source investigation at upstream v0.10.0 (`5d46f9c8`) confirmed built-in
  source-language rules in retain/fact_extraction.py and consolidation/prompts.py.
  Do not infer missing language instructions from bank missions alone. Directives
  apply to reflect, including reflect-based mental-model refresh, not retain or
  observation consolidation. `HINDSIGHT_API_LLM_OUTPUT_LANGUAGE` is the documented
  server-level control; it is absent from bank-configurable fields and remains
  prompt guidance, not output validation. Consolidation's separate dedup prompt
  omits both language rules, but was not the origin of the French update below.
- The live `/v1/default/banks/{bank}/llm-requests` traces on 2026-09-18 showed:
  retain request `c12554b4-19a0-482d-bb15-06e05cf1222b` received an English HLD
  chunk and the explicit no-translation rule, yet emitted eight Spanish facts.
  PRD retain request `0cbf2cac-b229-4e9d-9cd8-47626726f4a7` emitted French facts.
  Consolidation request `dacdcd50-bcbe-4108-bda7-212f9072ca02` received six English
  and two French new facts plus the per-observation source-language rule, then
  emitted three creates and six updates in French, including observation
  `8bd9d25b-c53f-42a1-81a7-7fbce9d66ff6`. These calls reported `gpt-5.6-luna`
  through `openai-responses`. The consolidation input trace was truncated, but
  retained the complete system prompt and new-facts section. Trace memory-ID
  filters return whole related operation runs, not just the responsible call;
  inspect actual outputs. Default trace retention is one day. This evidence
  supports model language drift and mixed-language batch spillover, not a missing
  bank mission requirement or a proven dedup cause.

- Language drift also occurs during extraction, not only consolidation:
  observation `989dfd33-d6fb-432d-863d-3562c9f5dfcf` is Spanish and derives from
  Spanish world fact `a175ae41-b4e4-48d0-a802-b7725e894bb0`. The retained original
  text of `hld.ingestion.initial-ingestion.0001` is English. Verified via the
  live API on 2026-09-18. Audit world facts as well as observations; rebuilding
  observations alone leaves incorrectly translated extracted facts in place.

- Hindsight v0.10.0 memory GET returns a deprecated, always-empty `history`
  field. Read `/v1/default/banks/{bank}/memories/{id}/history` for observation
  changes and resolved source facts. On 2026-09-18, observation
  `8bd9d25b-c53f-42a1-81a7-7fbce9d66ff6` in `stacked-chips-v2` changed from
  English to French during consolidation of English source facts. The live
  observations mission had no explicit output-language requirement. Check
  stored text, source facts, and history before blaming UI translation.
  A subsequent random sample of 20 other observations from all 446 found
  3 French and 17 English; the French entries were updated around
  04:36-04:37 UTC that day. This does not establish a configured language
  preference or the total number affected.
- The v0.10.0 `DELETE /memories/{memory_id}/observations` endpoint clears all
  observations derived from a source memory and automatically queues
  consolidation. It is not an observation-ID-only regeneration endpoint.
  Inspect shared source links before using it: the PRD source for the French
  observation above also supports an English observation. A normal bank
  consolidation trigger only processes unconsolidated memories.

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
- Survey path changes must account for persisted step descriptions as well as
  root metadata. Already-cooked prepare steps still pass `--output-dir`, and
  their survey steps name `<dir>/README.md`. Keep that CLI adapter and the saved
  `survey_doc`; new runs use `output_file` / `--output-file`. Do not migrate
  active worktree files: publish permits changes only to the saved document.
- On 2026-09-07 the installed `gc order run --var publish=none` preserved the
  override, contrary to the earlier integration assertion and survey warnings.
  Keep testing persisted descriptions and command dispatch, not just exit codes;
  older builds silently used defaults. The suite now checks both the override
  and default PR publication.
