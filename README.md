# Hindsight — `stacked-chips-v2`

The memory-bank design for the Stacked Chips platform — and the **Gas City
pack** that operates it. One bank spanning every repository and domain,
scoped down to a single rig on demand; a city-scoped archivist as the
bank's single writer.

Built against Hindsight **0.9.1**, server `https://hindsight-api.brandondennis.me`.

## Design documents

| File | Purpose |
|---|---|
| `agent-runbook.md` | **Hand this to an agent.** When and how to read and write the bank. No rationale. |
| `bank-template.json` | The bank manifest. Source of truth. Import this. |
| `deferred-mental-models.json` | Phase-2 mental models, held until the corpus can feed them. Not importable as-is. |
| `retain-contract.md` | What every writer must send on retain. The integration half, with rationale. |
| `rebuild.md` | Teardown, rebuild, bulk load, maintenance, cost map. |

```bash
hindsight bank import-template stacked-chips-v2 bank-template.json --dry-run
hindsight bank import-template stacked-chips-v2 bank-template.json
```

## The pack

| Item | Purpose |
|---|---|
| `pack.toml` | Pack manifest; declares the `archivist` city session |
| `agents/archivist/` | The bank's **single writer**. `max_active_sessions = 1` structurally enforces write serialization |
| `formulas/mol-hindsight-ship.toml` | Reconcile docs trees → bank (validate, ship changed, poll to terminal) |
| `formulas/mol-hindsight-consolidate.toml` | Nightly: drain ops, consolidate, tag audit, mental-model spot-audit |
| `orders/ship-sync.toml` | Hourly convergence backstop — makes all other ship triggers non-load-bearing |
| `orders/consolidate.toml` | Nightly consolidation + audit |
| `commands/ship/` | `gc` command: ship the docs manually at any point (auto-resolves roots; owns `--dry-run` for previews) |
| `assets/scripts/ship-docs.sh` | The single ship path, **stateless** (the bank is the ledger — stamped content hashes in `document_metadata`) and **ref-based** (ships committed content of `origin/HEAD`/`HEAD`, never the working tree). Contract enforcement, hash diff, drain-before-ship, op polling, GONE reports |
| `assets/scripts/bank-maintain.sh` | Deterministic maintenance: drain, consolidate (recover+retry), tag audit incl. Levenshtein near-duplicate detection. Exit codes drive the formula |
| `assets/scripts/memory-retain.sh` | The write path for **bank-native agent memories** (gotchas): contract payload, pending-op serialization, `--bump` for repeat reports (hit_count + timestamp refresh) |
| `skills/hindsight-memory/` | Read patterns for every agent: reflect/recall cadence, scoping, status semantics, `tag_groups` caveats |
| `skills/hindsight-shipping/` | Write patterns for authoring agents: frontmatter contract, gates, gotcha capture |
| `template-fragments/` | The **fragment contract**: `hindsight-brief` / `hindsight-propose` dispatchers consumers wire into any agent's prompt (see below) |
| `test/` | Rehearsal harness: dummy docs + validated prototype shipper + probe results from the 2026-08-25 live experiment |

Install into a city (path import while the pack is local):

```toml
[imports.hindsight]
source = "../hindsight"
```

referenced from `workspace.pack` so the archivist expands city-scoped.
Rig agents need no sessions from this pack — they consume the two skills.

## The shipping contract

The pack's public promise to any doc producer — platform docs, someone
else's docs, any schema — is four rules:

1. **Publish.** Put a doc with conforming frontmatter on your publish
   boundary — the canonical branch of a walked docs tree. It ships within
   the order interval (or immediately via the `ship` command).
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

Lifecycle policy is expressed by **where you commit**, not by pack
configuration. A draft you want visible platform-wide goes on the
canonical branch with `status: draft`; a draft not ready for anyone stays
on a private branch, invisible to the pack by construction. Branch =
privacy. Status = epistemic standing of published content. The pack has
no opinion and no knob.

> The walker reads the **committed content of each root's canonical ref**
> (`--ref` override → `origin/HEAD` → local `HEAD`), never the working
> tree — a branch switch or dirty worktree at order time cannot leak
> unpublished content into the bank, and untracked files are simply
> unpublished. A root outside any git repo falls back to walking the
> filesystem, so git is the recommended publish boundary, not a
> requirement.

**Status:** the bank design, retain contract, shipper logic, and query
patterns are validated against a live test bank (see `test/results/`).
The Gas City wiring — order scheduling, pool routing to the archivist,
formula dispatch — follows the gastown pack's conventions but has not yet
been exercised in a running city. Treat `pack.toml`, `orders/`, and
`agents/archivist/agent.toml` as reviewed-not-run.

Deliberately not in the pack (yet): a git-hook ship trigger (optional
immediacy, never load-bearing — add per-repo later if wanted), bulk
backfill automation (rehearse via `test/` + `rebuild.md` instead), and any
per-rig session or per-repo mental models (per-repo observation scopes
already provide that lens).

Known coupling, planned split: the stacked-chips doc schema (the `type`
table, status/source/scope vocabularies, and tag derivation from
`repos`/`domains`) is currently hard-coded in `ship-docs.sh`. The
intended end-state is a pluggable schema layer — the pack core owns the
universal mechanics (ref walk, hash diff, drain, metadata stamping, GONE)
and ships whatever tags the schema derives, without interpreting them;
`document_metadata` stays the pack's private bookkeeping either way.

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
| `hindsight-arbitrate` | The archivist's acceptance policy: dedup-first (bump `hit_count` on repeat reports), four gates, deny-biased. Wired by this pack onto its own archivist; no gate var |

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
| `HINDSIGHT_API` | `[workspace] env`, optional | API base URL override for the CLI |
| `HINDSIGHT_MEMORY` | per-agent `env` / patch | Set non-empty to render `hindsight-brief`. THE opt-in switch |
| `HINDSIGHT_PROPOSE` | per-agent `env` / patch | Set non-empty to render `hindsight-propose` |
| `HINDSIGHT_MENTAL_MODELS` | per-agent `env`, optional | Space-separated mental-model ids fetched at session start |
| `HINDSIGHT_ARCHIVIST` | per-agent `env`, optional | Mail target for proposals (default `archivist`; set when the import binding qualifies the name) |

### Wiring recipes

Shared plumbing, once per city:

```toml
[workspace]
env = { HINDSIGHT_BANK = "stacked-chips-v2" }
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
reports face four deny-biased gates (verified / a trap / expensive when
sprung / durable — `hindsight-arbitrate` is the policy, the
`hindsight-shipping` skill the mechanics); accepted memories are
retained by `memory-retain.sh` — `type: gotcha` today, and the pipeline
is type-agnostic.

The bank has two write paths, one per kind of truth:

| | Docs corpus | Agent memories |
|---|---|---|
| Source of truth | git trees (canonical refs) | the bank itself |
| Write path | `ship-docs.sh` sync | `memory-retain.sh` via arbitration |
| Git involved | yes — commit is publish | no — like voice memos |
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
  1. Delete the file and commit the removal (else the next sync
     re-ships it — the bank converges on the tree).
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
somewhere else, and the exceptions get written down deliberately as `gotcha`
documents rather than harvested from transcripts.

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
`retain-contract.md`.

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
hit. See the cost map in `rebuild.md`.

### 3. Eleven retain strategies, selected by declaration

A document declares `type` once in frontmatter. The shipper derives the
`memory_type:` tag, the `source:` tag, **and** the strategy from that one field.
Authors never choose a strategy and never remember when one is needed.

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
defaults with no error. The shipper must refuse unknown `type` values.

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

The saving grace: every document lives in git with a stable `document_id`, so
any extraction mistake is recoverable by re-retaining from source. That is a
rebuild script, not data loss — and it is a luxury a session-sourced bank does
not have.

Full cost map in `rebuild.md`.
