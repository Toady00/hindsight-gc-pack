# Hindsight memory pack

Shared platform memory on Hindsight, as a **Gas City pack** — a building
block for OMG, platform-agnostic by design. One bank spanning every
repository and domain, scoped down to a single rig on demand; a
city-scoped archivist as the bank's single writer.

Templates were built against local Hindsight **0.9.1/0.9.2** servers. Document
shipping requires `/openapi.json` to advertise client `operation_id` and boolean
`async` on `RetainRequest`; unsupported servers are refused before retain.
No server is defaulted. Endpoint precedence is `--api`, `HINDSIGHT_API`,
`HINDSIGHT_API_URL`, then the selected CLI profile/config, otherwise refusal.
`HINDSIGHT_PROFILE` selects `~/.hindsight/cli-profiles/<name>.toml`; without a
profile, `HINDSIGHT_CONFIG` selects a file or defaults to `~/.hindsight/config`.
Credentials come from `HINDSIGHT_API_KEY`, or the config only when its endpoint
is used. Explicit endpoints never borrow another endpoint's config credentials.

## The bank template

`example.bank-template.json` is the working starting point: the bank
missions and dispositions, the eleven retain strategies the default
`docs` schema emits, the six directives protecting the bank's core
distinctions, and the four mental models the pack itself references
(`conventions-and-standards` and `landmines` feed the nightly audit;
the service map and open-risks models are the coordinator briefs in
the wiring recipes). Copy it, extend it with your own mental models,
import it:

```bash
hindsight bank import-template <bank> example.bank-template.json --dry-run
hindsight bank import-template <bank> example.bank-template.json
```

Re-importing after edits is safe and immediate for missions,
directives, dispositions, and mental models; retain strategies govern
extraction and apply forward-only (see "Two properties worth knowing").

## The pack

| Item | Purpose |
|---|---|
| `pack.toml` | Pack manifest; declares the `archivist` city session |
| `agents/archivist/` | The bank's **single writer**. `max_active_sessions = 1` structurally enforces write serialization |
| `agents/surveyor/` | Rig-scoped, scale-to-zero pool that authors a rig's `current-state` survey. Reads code in an isolated worktree; every mechanical step is `survey.sh` |
| `formulas/mol-hindsight-ship.toml` | Reconcile docs trees → bank (validate, ship changed, poll to terminal) |
| `formulas/mol-hindsight-consolidate.toml` | Nightly: audit first (tags, config drift, mental-model overreach), consolidation as ensure-and-wait backstop — auto-consolidation handles freshness |
| `formulas/current-state-survey.toml` | v2 workflow: worktree → survey → publish (`pr` default, `direct`, `none`) → teardown cleanup. Step prompts in `assets/workflows/current-state-survey/` |
| `orders/ship-sync.toml` | Hourly convergence backstop — makes all other ship triggers non-load-bearing |
| `orders/consolidate.toml` | Nightly audit + consolidation backstop |
| `orders/current-state-survey.toml` | Monthly per-rig re-survey; `gc order run current-state-survey --rig <rig>` fires it by hand |
| `commands/ship/` | `gc <binding> ship` — ship the docs manually at any point (auto-resolves roots; owns `--dry-run` for previews). Pack commands are namespaced by the import binding, so `[imports.hindsight]` makes it `gc hindsight ship` |
| `commands/survey/` | `gc <binding> survey prepare\|show\|stamp\|publish\|cleanup <root>` — the survey's mechanical steps, shared by the formula and humans |
| `commands/read/`, `commands/maintain/`, `commands/retain/`, `commands/status/` | `gc hindsight read`, `gc hindsight maintain`, `gc hindsight retain`, `gc hindsight status`: reads, guarded maintenance and bank-native retention, and shared Beads health |
| `assets/scripts/ship-docs.sh` | Git-only docs shipping from freshly fetched origin branches. Shared Beads receipts track intent, recovery and confirmed completion; bank hashes alone never prove success. Validation, drain-before-ship, operation polling and GONE reports |
| `assets/scripts/bank-maintain.sh` | Deterministic maintenance: drain, consolidate (recover+retry), tag audit incl. Levenshtein near-duplicate detection. Exit codes drive the formula |
| `assets/scripts/memory-retain.sh` | The write path for **bank-native agent memories** (gotchas): contract payload, pending-op serialization, `--bump` for repeat reports (hit_count + timestamp refresh) |
| `assets/scripts/survey.sh` | Survey mechanics: worktree under `<city>/.gc/worktrees/<rig>/`, validated `status: draft` / `source: agent` frontmatter, push/PR via `gh` or `glab`, safety-gated cleanup. Handoff state lives on the workflow root bead |
| `schemas/` | Pluggable doc dialects: `docs` (the pack's frontmatter contract, the default) and `null` (raw passthrough). Each is `derive` + `audit-vocab.json` — see "The schema layer" |
| `skills/hindsight-memory/` | Read patterns for every agent: reflect/recall cadence, scoping, status semantics, `tag_groups` caveats |
| `skills/hindsight-shipping/` | Write patterns: frontmatter contract (the default `docs` dialect), approval recording, agent-contributed memory mechanics |
| `template-fragments/` | The **fragment contract**: `hindsight-brief` / `hindsight-propose` / `hindsight-arbitrate` dispatchers consumers wire into agents' prompts (see below) |
| `test/` | Rehearsal harness: dummy docs + validated prototype shipper + probe results from the 2026-08-25 live experiment |

Install into a city (path import while the pack is local):

```toml
[imports.hindsight]
source = "../hindsight"
```

Place that import in `city.toml` for the city-scoped archivist. To enable
surveys on a rig, also add `[rigs.imports.hindsight]` to that rig's existing
`[[rigs]]` entry, with `source` pointing to this pack. The rig import exposes
the survey order; the surveyor stays idle until work is routed to it.
The formula addresses the surveyor as `hindsight.surveyor`, so the import
binding must be `hindsight`. See the [live survey test](test/SURVEY.md) for
configuration and verification steps.

## Using this pack

Every path in one place. Details for each live in the sections below.

**Operator — install and wire (once per city):**

1. Import the pack (above) and create the bank:
   `hindsight bank import-template <bank> example.bank-template.json`
2. Declare the bank in `city.toml` — required, no default anywhere:
   `[workspace] env = { HINDSIGHT_BANK = "<bank>" }`
   (add `HINDSIGHT_API` if not using the CLI's configured server)
3. Opt agents into memory: `append_fragments`/`inject_fragments_append`
   + the gate env vars per agent — see "Wiring recipes"
4. The orders take it from there: hourly ship sync, nightly audit. The
   archivist wakes only when work arrives.

**Doc author (human or agent) — publish a doc:**

Write markdown with the frontmatter contract (see the
`hindsight-shipping` skill), put it in a configured Git docs tree, then commit
and push to the canonical branch. Push publishes it for the next scan, including
`status: draft` documents. `gc hindsight ship` queues the archivist outside its
managed session; exit 0 means queued, not shipped. Follow the formula bead for
completion. `gc hindsight ship --dry-run` previews locally without retaining or
updating health, but still fetches Git and reads the bank and city Beads store.
The first word after `gc` is your import binding name.
Revise by editing in place; retire with `status: deprecated` — never
delete.

**Operator - survey a rig:**

For the first live test, create a local-only run from the city directory:

```bash
gc formula cook current-state-survey --rig <rig> --var publish=none --json
```

This starts real agent work and creates a signed local commit, but does not
push or call Hindsight. The worktree remains for inspection. Do not start a
second survey while one is active. The installed `gc order run` accepts
`--var` but silently drops its values; do not use that path to suppress
publishing. The scheduled order and `gc order run current-state-survey
--rig <rig>` use the default PR publication mode.

Generated surveys are `status: draft` and `source: agent`, including PRs.
A merge does not automatically change those fields. Human review can promote
the status separately; a current-state survey records observations, not an
adopted design decision. Draft documents on the canonical branch still ship
through the normal Git ingestion path. See [test/SURVEY.md](test/SURVEY.md)
for prerequisites, acceptance checks, and recovery.

**Reading agent — query memory:**

If the operator opted you in, your prompt already carries the brief:
one scoped `reflect` at task start, `recall` freely during work,
mental models for standing answers. The `hindsight-memory` skill has
the full patterns. No setup on your side.

**Worker who hit a landmine — propose a memory:**

You do not write to the bank. Mail the archivist (your prompt's
`hindsight-propose` fragment has the format and the self-filter). The
archivist checks scope (platform knowledge only — city-operations
lore belongs in AGENTS.md, not the bank), dedups, applies the four
gates, and retains or replies why not. Repeat reports bump the
memory's hit count instead of duplicating it.

**Depending pack author (e.g. a product pack with its own agents):**

Import this pack from yours (`[imports.hindsight]` — transitive, the
city gets everything). Wire your agents via your own `agent.toml` env +
fragments, patch agents from other packs with `[[patches.agent]]`, and
specialize prose by defining `hindsight-brief-<agent>` fragments. Want
your own doc dialect? Ship a `schemas/<yours>/` directory and select it
per run — see "The schema layer".

**Different conventions entirely:**

`gc hindsight ship --schema schemas/null` reads raw `hindsight:` frontmatter blocks —
no vocabulary, no protection, you own the invariants. One schema per
run; different dialects belong in different cities and banks.

**Operator — maintenance and repair:**

The nightly order audits (tag hygiene, config drift, mental-model
overreach) and runs a consolidation backstop; findings reach you by
mail from the archivist. Removing something on purpose is manual by
design — see "Deleting from the bank".

`gc hindsight status` reads shared Beads health and unresolved document attempts
without calling the bank. Use `gc hindsight maintain` for maintenance and
`gc hindsight retain` for arbitrated bank-native memories, only in the managed
archivist. Use `gc hindsight read` for read operations. See
[OPERATIONS.md](OPERATIONS.md) for connection settings, bootstrap and recovery.

## The shipping contract

The pack's public promise to any doc producer — platform docs, someone
else's docs, any schema — is four rules:

1. **Publish.** Push a doc with conforming frontmatter to the canonical branch
   of a configured Git docs tree. The next scheduled scan or queued
   `gc hindsight ship` request reconciles it; check the result for completion.
2. **Revise.** Change it there and it re-ships, replacing its previous
   self in the bank (`document_id` replace). Change detection is a content
   hash of the whole file, so *any* change ships — frontmatter included.
3. **Retire.** Edit the doc to say it is dead (in this platform's schema:
   `status: superseded` or `deprecated`) and leave the file in place. It
   re-ships as its own tombstone. Nothing is ever deleted from the bank;
   a file that vanishes gets a GONE report, never a removal.
4. **Everything else is not this pack's business.** What "draft" means,
   who approves, when status flips — that belongs to your doc schema and
   your workflow pack, and reaches this pack only as content changes.

Publication is determined by **where you push**. A draft pushed to the canonical
branch with `status: draft` is visible. Status records the standing of published
content; it does not gate ingestion, and no accepted-source filter is added.
A private or unpushed branch is excluded from the default scan. An explicit
`--ref` can select another published branch, so do not use it for private drafts.

Every scan freshly fetches `origin`, including dry-runs. The default branch
comes from the remote's advertised HEAD, not a cached `origin/HEAD` or local
HEAD. `--ref` accepts a published branch as `foo`, `origin/foo`, `refs/heads/foo`
or `refs/remotes/origin/foo`; local HEAD, SHAs, tags and local-only branches are
unsupported. `--fetch` is retained for existing callers but is a no-op because
all scans fetch. The shipper reads the fetched commit's blobs, never worktree
content. Branch switches, dirty files and untracked files are irrelevant.

Every root must belong to Git, including worktrees; there is no filesystem
fallback. Automatic scans include every registered rig's `docs/` even when
absent from its checkout, plus `HINDSIGHT_DOCS_ROOTS`. City-root `docs/` is
also scanned when the city belongs to Git, even if absent locally. A missing rig
repository fails coverage; docs absent from the fetched tree form a valid empty
inventory for GONE checks. Any fetch, tree-listing or blob-read failure stops the
entire scan before new retains. Source records for the Git docs/code corpus
carry stable origin-based repository identity, relative path, branch and commit
SHA. Bank-native memories remain a separate write path.

### Shared shipping ledger

Shipping is not stateless. Durable Beads records in the shared city SQL database
hold document attempts and successful receipts as custom type
`hindsight-document`, and bank health as `hindsight-bank`. Both remain lifecycle
status `pinned`, unassigned and unrouted, not ready tasks. Commands explicitly
address the city store through absolute `GC_CITY_PATH`, never cwd-based rig
routing. Normal metadata updates need no force. There is no shared lock;
exactly one active archivist must own each writable bank.

`metadata.hindsight` holds `schema_version: 1` and `api`, `bank`, `kind`,
`document_id` identity. Its `.data` contains document `attempt`, `last_success`
and `source`, or bank `latest_run`, `latest_full_scan` and `last_success`.
Before retain POST, the shipper persists a UUID operation intent and full request
snapshot. It confirms completion before recording success. The next run recovers
the original unresolved operation with bounded retry, even after a machine
handoff. Successful completion removes the payload from current metadata, but
Dolt history retains earlier values, including source content.

A matching bank content hash can survive a failed streaming retain. Skipping
therefore requires a matching successful Beads receipt as well as the bank hash.
For missing receipts or unknown success, review `gc hindsight ship --dry-run`,
then run a real scan to re-ingest once and establish receipts. Invalid records
require repair, not blind bootstrap. `gc hindsight status` queries this shared
store, not the live bank, for freshness and unresolved attempts. Partial scans
cannot refresh the successful full-scan timestamp. There is no
`HINDSIGHT_STATE_DIR` health journal to copy between hosts.

For extraction-setting changes, preview and request `gc hindsight ship
--reprocess`. It retains current source first, then calls the server's document
reprocess endpoint. The endpoint is not caller-idempotent. A lost acknowledgement
leaves phase `reprocess_prepared` and blocks recovery until a human identifies
the actual operation and repairs `metadata.hindsight.data.attempt` with
`reprocess_operation_id` and phase `reprocess_wait`. Never blindly retry the
POST. This requests reprocessing, not a verified guarantee of server-side
unchanged-chunk invalidation. See [OPERATIONS.md](OPERATIONS.md) for repair details.

**Verification:** `test/results/` records the original live-bank experiment,
not proof of the current shared-Beads recovery and Git-only shipping path.
The offline suite exercises production scripts with mocked services; see
[test/README.md](test/README.md) for its unchanged command and the separate
optional live evaluation. Order scheduling and archivist routing still need
verification in a running city; lint and offline tests do not prove that wiring.

Deliberately not in the pack (yet): a git-hook ship trigger (optional
immediacy, never load-bearing — add per-repo later if wanted), bulk
backfill automation (rehearse via `test/` on a disposable bank), and any
per-rig session or per-repo mental models (per-repo observation scopes
already provide that lens).

## The schema layer

The core owns mechanics (ref walk, hash diff, drain, serialization,
metadata stamping, GONE); a **schema** owns the dialect — what counts as
a doc, its validation, and the derived retain fields. A schema is a
directory:

```
schemas/<name>/
├── derive            # executable: doc on stdin → one JSON verdict
└── audit-vocab.json  # tag axes + closed vocabularies for the nightly audit
```

`derive` emits `{"verdict":"skip"}` (not this schema's doc),
`{"verdict":"refuse","document_id":…,"reason":…}`, or
`{"verdict":"ship", document_id, strategy, tags, observation_scopes,
context, timestamp}` (optional `content` overrides the default
frontmatter-stripped body). Warn-level findings go to stderr. The core
provides city context via `HINDSIGHT_KNOWN_REPOS` /
`HINDSIGHT_KNOWN_DOMAINS_FILE`. The audit reads `audit-vocab.json` from
the same directory, so write-side dialect and audit vocabulary are one
artifact and cannot drift.

Selection: `--schema <dir>` → `$HINDSIGHT_SCHEMA` → the pack's default,
`schemas/docs` (the frontmatter contract above). **One schema per run,
per-city by design**: different
dialects belong in different cities and banks. Two schemas writing one
bank share tag space with no guarantee their vocabularies mean the same
things — the schema layer makes writers pluggable, not vocabularies
compatible.

Also bundled: `schemas/null` — raw passthrough for users with their own
conventions (`hindsight:` frontmatter block carrying the retain fields
directly). No vocabulary, no derivation, no audit vocab, no protection:
you own tag hygiene and observation-scope discipline. History says raw
blocks rot; prefer a real dialect when one fits.

A depending pack ships its own dialect by adding one directory (its
`derive` + `audit-vocab.json`) plus the read-side skills/fragments that
speak it — no core changes.

## The fragment contract

The pack's second public API, for prompt injection: named template
fragments any consumer wires into any agent. The pack publishes
capability; **the layer that owns an agent decides who gets injected**
(a depending pack for its own agents, a pack patch for agents in its
import subtree, the city for anything). The pack never decides.

Published names (stable API — renaming one is a breaking change):

| Fragment | Renders |
|---|---|
| `hindsight-brief` | Read-side: the bank exists, the one-reflect task-start ritual, optional standing mental models, skill pointer |
| `hindsight-propose` | Write-side: self-filter + the four-field proposal mail to the archivist, who arbitrates retention (single-writer preserved) |
| `hindsight-arbitrate` | The archivist's acceptance policy: scope check (platform knowledge, not city-ops), dedup-first (bump `hit_count` on repeat reports), four gates, deny-biased. Wired by this pack onto its own archivist; no gate var |

All are dispatchers; `brief` and `propose` are additionally gated on a
per-agent env var. Each tries the most specific registered
specialization first, then falls back:

    hindsight-brief-<AgentName>      qualified ("myrig/polecat-1")
    hindsight-brief-<TemplateName>   config name ("delivery" — one
                                     define covers a whole pool)
    hindsight-brief-default          shipped by this pack

Any importing pack or city can define a specialization in its own
`template-fragments/` — this pack never knows. City-root fragments win
on name collision, so operators can also replace the defaults outright.
A missing specialization falls through silently to the default; when a
custom fragment is not rendering, check the name with `gc prime <agent>`.

### Env contract

Declared in TOML like all city config; env is the delivery mechanism.

| Var | Where | Meaning |
|---|---|---|
| `HINDSIGHT_BANK` | `[workspace] env` — once per city, **required** | Bank id. No hardcoded default anywhere — scripts, command, and formulas fail loudly when unset. Process-env only (workspace env is not template-visible), which is why fragment prose references it as `$HINDSIGHT_BANK`. 1:1 city-to-bank is the intended shape; a per-agent `env` override is the escape hatch |
| `HINDSIGHT_API` | `[workspace] env`, optional | API base URL. Resolution: `--api`, this var, `HINDSIGHT_API_URL`, selected CLI profile/config, then refusal. No default server; see connection settings above |
| `GC_CITY_PATH` | Gas City command/session environment | Absolute city path selecting the shared city Beads SQL store for document receipts and bank health; no rig-cwd or local journal fallback |
| `HINDSIGHT_DOCS_ROOTS` | `[workspace] env`, optional | Whitespace-separated docs roots added to the ship sync's auto-resolution — the channel for standalone docs repos that are not rigs. Managed sessions inherit it, so the scheduled order covers them |
| `HINDSIGHT_MEMORY` | per-agent `env` / patch | Set non-empty to render `hindsight-brief`. THE opt-in switch |
| `HINDSIGHT_PROPOSE` | per-agent `env` / patch | Set non-empty to render `hindsight-propose` |
| `HINDSIGHT_MENTAL_MODELS` | per-agent `env`, optional | Space-separated mental-model ids fetched at session start |
| `HINDSIGHT_ARCHIVIST` | per-agent `env`, optional | Mail target for proposals (default `archivist`; set when the import binding qualifies the name) |

### Wiring recipes

Shared plumbing, once per city:

```toml
[workspace]
env = { HINDSIGHT_BANK = "omg" }
```

Your own agent (a depending pack's `agent.toml`):

```toml
append_fragments = ["hindsight-brief"]
env = { HINDSIGHT_MEMORY = "1", HINDSIGHT_MENTAL_MODELS = "product-context landmines" }
```

An agent from a pack you import (pack- or city-level patch):

```toml
[[patches.agent]]
name = "mayor"
inject_fragments_append = ["hindsight-brief"]
env = { HINDSIGHT_MEMORY = "1" }
```

A worker pool (patch the template agent; instances inherit):

```toml
[[patches.agent]]
name = "polecat"
rig = "*"
inject_fragments_append = ["hindsight-propose"]
env = { HINDSIGHT_PROPOSE = "1" }
```

Custom prose for one agent, no pack edits — define in any
`template-fragments/` the agent can see:

```
{{define "hindsight-brief-delivery"}}...your prose...{{end}}
```

Because injection rides the prompt template, it is delivered at session
start and re-delivered after compaction on every harness gc manages
(claude/codex re-prime via hooks and handoff restarts; opencode
re-injects the prime into the system prompt each turn).

## Agent-contributed memory

The pipeline for what agents learn the hard way: **proposal mail →
arbitration → bank-native retain**. Workers never write; they mail the
archivist (`hindsight-propose` carries the format and self-filter). New
reports face a scope check (about the platform, not the machinery
operating the city) and four deny-biased gates (verified / a trap /
expensive when sprung / durable — `hindsight-arbitrate` is the policy,
the `hindsight-shipping` skill the mechanics); accepted memories are
retained by `memory-retain.sh` — `type: gotcha` today, and the pipeline
is type-agnostic.

The bank has two write paths, one per kind of truth:

| | Docs corpus | Agent memories |
|---|---|---|
| Source of truth | git trees (canonical refs) | the bank itself |
| Write path | `ship-docs.sh` sync | `memory-retain.sh` via arbitration |
| Git involved | yes, push to the canonical branch publishes | no, like bank-native voice memos |
| GONE detection | yes (`document_metadata.repo`) | exempt (`kind: agent-memory`, no repo metadata key) |

Repeat reports `--bump` the memory: `hit_count` increments (metadata
and a reader-visible `Reported N times` line in the content) and the
re-retain refreshes the bank timestamp, so a frequently-hit issue stays
fresh exactly as often as it actually bites while a stale one ages out
of recency naturally. A rising count on an existing memory means the
memory is not landing — a retrieval/injection deficiency signal,
queryable from the documents list.

Retrieval scoping is unaffected by residency: agent memories carry full
`repo:` tags — that is how rig agents discover them.

## Deleting from the bank (manual, break-glass)

The pack never deletes — retire-in-place (`status: deprecated`) is the
in-band mechanism, and the archivist's hard rules forbid destructive
ops. But a human removing pure noise (a premature doc, a pivoted
direction) is legitimate. The procedure differs by kind:

- **Bank-native memory** (gotcha, out-of-band voice memo):
  `hindsight document delete <bank> <doc-id>` — clean and final. No
  file exists, nothing resurrects it.
- **Tree-backed doc**: two halves, or it comes back.
  1. Delete the file, commit and push the removal to the canonical branch
     (else the next sync re-ships it from the fetched tree).
  2. `hindsight document delete <bank> <doc-id>` (else the bank doc
     lingers and the GONE report flags it every run).

  Do the file first. A lingering GONE line is the "you left the job
  half-done" reminder, by design — report-only, never auto-removed.

Deletion erases the bank's copy only; anything else that referenced the
doc id in body text keeps its dangling reference. Prefer deprecation
whenever the history has value.

---

## What this bank is for

**The corpus is authored documents, not agent sessions.** PRDs, specs, ADRs,
HLDs, user journeys, methodologies, conventions, meeting notes, current-state
surveys, build reports, gotchas, and owner voice memos.

Agent-to-agent conversation is explicitly out. A three-week orchestrator session
is noise — everything durable in it is a distillation of something a human said
somewhere else, and the exceptions arrive deliberately as arbitrated `gotcha`
memories (see "Agent-contributed memory") rather than harvested from
transcripts.

This inverts the assumption most Hindsight tuning starts from. The bank default
is `verbose` extraction, not `concise`, because the corpus is dense authored
technical prose where losing nuance is the failure mode — not conversation,
where concise is correct.

---

## The three design decisions

### 1. One bank, scoped by tag

Per-repo banks would kill the query that matters most — *"what did we decide
about billing across the platform"* — because Hindsight has no cross-bank
visibility.

Scoping is an OR across an explicit reach marker and the repo tag:

```
--tags repo:svc-payments,scope:platform --tags-match any_strict
```

`tags_match: "any"` will not work for this. It includes memories with **no tags
at all**, and a cross-cutting HLD carries `scope:`, `domain:`, `memory_type:`,
and `source:` — so it is tagged, so it never matches a repo-scoped query.
`any_strict` matches any listed tag while excluding untagged, which is what makes
the explicit `scope:platform` marker visible from inside a repo. Details in
the `hindsight-memory` skill.

### 2. Six tag axes, all writer-stamped

`scope:` · `repo:` · `domain:` · `memory_type:` · `source:` · `status:`

Every one is known from frontmatter before retain, so every one is a
deterministic tag. None are LLM-generated, and none require registration —
adding a new repo or domain costs nothing.

**There are no `entity_labels`, deliberately.** An entity label is a *per-fact*
classification the LLM assigns during extraction; it earns its place only for a
distinction the writer cannot know at retain time, such as which sentences
*inside* a spec are constraints versus rationale. All six axes above are
document-level, so none of them qualify. Entity labels are also the most
expensive thing to get wrong here — existing entities are never reclassified —
so the group to add is the one aimed at a retrieval failure you have actually
hit.

### 3. Eleven retain strategies, selected by declaration

A document declares `type` once in frontmatter. The schema
(`schemas/docs/derive`) derives the `memory_type:` tag **and**
the strategy from that one field. Authors never choose a strategy and
never remember when one is needed.

The strategies partition by **extraction behavior**, not document taxonomy —
taxonomy is what `memory_type:` is for:

| | |
|---|---|
| `design-record` | ADR / spec / HLD — decisions, alternatives, constraints, interfaces |
| `product-doc` | PRD / user journey — problem, users, outcomes, non-goals |
| `methodology` | calculation and analytical methods — formulas preserved exactly |
| `convention` | tools, languages, practices — ranked preferences kept intact |
| `current-state` | brownfield survey — observed, not decided |
| `discussion` | meeting notes — decided vs. proposed, kept apart |
| `voice-memo` | owner thinking — never a settled decision |
| `build-report` | what actually shipped, and how it deviated |
| `operational-runbook` | running-system procedures, verbatim |
| `gotcha` | the landmine and how to avoid it |
| `source-document` | third-party material, verbatim |

**A strategy name that does not exist is silently ignored** — you get bank
defaults with no error. That is why the schema refuses unknown `type`
values instead of passing them through.

---

## Three distinctions the bank is built to protect

Everything else follows from these. Each has a strategy separating the inputs
and a directive keeping them apart at reflect time.

1. **Decided vs. proposed vs. thinking.** An ADR outranks a position floated in a
   meeting, which outranks an idea in a voice memo. Collapsing these means the
   bank starts confidently asserting things nobody agreed to.

2. **Specified vs. built vs. observed.** What a spec required, what a build
   report says shipped, and what a survey found are three different claims. When
   they diverge, that divergence *is* the finding — reconciling it silently
   destroys the most valuable thing in the bank.

3. **Platform-wide vs. repo-local.** In a shared bank the failure mode is a
   convention from one repo quietly becoming a platform standard. The
   `scope:`/`repo:` axes and the "Name the scope of every claim" directive exist
   for exactly this.

---

## Two properties worth knowing before you change anything

**Directives, dispositions, `reflect_mission`, and every mental model are free to
change.** They govern reasoning over existing memories. Re-import and they take
effect immediately.

**Retain strategies and `entity_labels` are forward-only.** They govern
extraction, so changing one affects only future retains. Applying it to existing
content requires reprocessing each document — which works only while
`store_document_text` stays at its default `true`. That is the one irreversible
setting here, which is why it is deliberately absent from the manifest.

Git-backed documents retain their stable `document_id`, so source remains
available for a reviewed re-ingestion or `--reprocess` request. Re-retaining
alone is not a guarantee that a server re-extracts unchanged chunks. Follow the
reprocess recovery procedure above if an acknowledgement is lost. Bank-native
agent memories are the exception to Git recovery: back them up with the bank's
export tooling before any teardown.
