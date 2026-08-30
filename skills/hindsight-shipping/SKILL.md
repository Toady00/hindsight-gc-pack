---
name: hindsight-shipping
description: Getting documents into the platform memory bank — frontmatter contract, the ship path, approval recording, and agent-contributed memory via archivist proposals
---

# Shipping documents to the memory bank

The bank converges on the docs tree: **anything with valid frontmatter
ships, drafts included**. You get content into the bank by writing a doc
and committing it — not by calling the Hindsight API. The archivist's
scheduled sync (`ship-docs.sh`) is the single ship path.

Authoritative contract: `retain-contract.md` in the hindsight pack. This
skill is the working subset for authoring agents.

## Frontmatter every shippable doc needs

```yaml
---
schema_version: 2
id: spec.eventing.transport.0001    # stable, unique; this IS the bank document_id
type: spec                          # see type table below
title: Event Transport for Widget Processing
status: draft                       # draft | accepted | superseded | deprecated
source: agent                       # human | agent | external
scope: platform                     # business | platform | repo
repos: [svc-widgets]                # rig names; omit if none
domains: [eventing]                 # bounded contexts; omit if none
created_at: 2026-08-20T00:00:00Z
updated_at: 2026-08-20T15:30:00Z    # bump on every revision; drives re-ship
---
```

Types: `adr` `spec` `hld` `prd` `user-journey` `methodology` `convention`
`current-state` `meeting-notes` `discussion` `voice-memo` `build-report`
`runbook` `gotcha` `external`. Anything else is refused at ship time.
Omit `status` entirely for current-state, build-report, gotcha,
voice-memo, external.

Rules that bite:

- **One status vocabulary for every doc type.** Not "final", not
  "approved", not "proposed" — `draft | accepted | superseded | deprecated`.
- **`source` is human-in-the-loop for this revision**, not authorship
  history. You (an agent) write `source: agent`. It becomes `human` only
  when a person writes or approves the revision. If you revise a
  human-approved doc, revert it to `agent` (and `status` to `draft`
  unless your workflow is trusted to re-accept).
- **`repos` values are rig names** — the city rig list is the registry.
  **`domains` are bounded contexts** — never path-taxonomy values like
  `platform` (that's a scope). Copy existing spellings; never mint
  variants.
- **Docs are never deleted** — mark `deprecated` (or `superseded`) and
  let the sync re-ship them. Cross-reference related doc ids in the body
  (frontmatter is stripped at ship; body text is what survives into
  memory).

## Recording approval — the contract rules

*How* approval happens (gates, reviews, sign-off flows) belongs to your
workflow pack, not this one. This pack only defines what a recorded
verdict must look like in frontmatter:

- A human approving a revision sets `status: accepted` **and**
  `source: human` together, bumps `updated_at`, commits, and ships (the
  `ship` command, or let the hourly sync catch it).
- Only record an approval a human actually gave, with traceable
  provenance. Never infer one.
- Revising a human-approved doc reverts `source:` to `agent` (and
  `status:` to `draft` unless your workflow is trusted to re-accept).
- Trusted workflows explicitly permitted to accept their own specs set
  `status: accepted` + `source: agent` — that pair means exactly
  "standing position, never human-reviewed."
- Rejection is not a status: the doc simply stays `draft`; feedback lives
  in your workflow's work tracking, not in frontmatter.

## Agent-contributed memory — proposals, not direct writes

When something costs you real time and would cost the next agent the
same, you do NOT write it to the bank or the docs tree yourself. Mail a
memory proposal to the archivist (the `hindsight-propose` fragment in
your prompt carries the format and the self-filter). The archivist
arbitrates: checks for existing coverage, applies the acceptance gates,
and authors the doc if it clears the bar. One filter, one writer.

Do NOT harvest proposals from session transcripts, and never propose
progress notes, tool output, or coordination chatter — the never-retain
list in the contract is absolute.

### Archivist mechanics (arbitration accepted → doc)

Agent-contributed docs live in the **platform docs repo** (not rig
trees — you are the only committer there for this purpose), tagged
`repos:` for the rigs they bite; retrieval scoping rides the tags, not
the file's location. Usually `type: gotcha`; pick the type that fits
the content — the type table is the menu, and new agent-memory types
are added to the schema deliberately, not improvised mid-arbitration.

```markdown
---
schema_version: 2
id: gotcha.tflint-provider-cache-lock
type: gotcha
title: tflint provider cache lock
source: agent
scope: repo
repos: [terraform-platform]
hit_count: 1                # times agents reported hitting this
created_at: <now>
updated_at: <now>           # bump on EVERY hit — keeps the bank timestamp fresh
---
## Symptom
<exact error text>
## Cause
<what was actually wrong>
## Fix
<the correct approach, exact commands>
## Applies to
<where this bites>

Reported 1 time (last: <date>).
```

The trailing report line is body text on purpose: it survives into
retained content, so agents whose reflect surfaces this memory also see
how often it bites.

**Dedup check** (before judging any proposal):

```bash
rg -il "<symptom key terms>" <docs-repo>/ --glob '*.md'   # lexical, finds the file to edit
hindsight -o json memory recall "$HINDSIGHT_BANK" "<symptom>" \
  --tags repo:<rig> --tags-match any_strict --budget low --max-tokens 1024 \
  > "$TMP/dedup.json" 2>/dev/null && jq -r '.results[]?.text' "$TMP/dedup.json"
```

**Bump on a repeat hit** (fully covered, or covered-plus-merge): edit
the doc — increment `hit_count`, update the body's report line, merge
any new information, bump `updated_at` — commit (one commit per
verdict, message naming the reporter and mail id), and ship. The
whole-file hash makes even a count-only change re-ship, which refreshes
the bank's timestamp: a frequently-hit issue stays fresh, and one whose
truth has drifted ages out of recency naturally. A rising `hit_count`
on an existing memory means the memory is not landing — that is a
system-deficiency signal, visible with
`rg "hit_count:" <docs-repo>/ --glob '*.md'`.

## If you must call the ship script yourself

Formula final steps may run the sync for immediacy:

```bash
<pack>/assets/scripts/ship-docs.sh <docs-root>   # bank from $HINDSIGHT_BANK
```

It validates, skips unchanged docs, serializes per document (never races a
pending operation — racing orphans memories permanently), and polls
operations to terminal. Exit non-zero means refused or failed docs; read
the report, fix the doc, never work around the validator. Direct API
retains outside this script are forbidden.
