---
name: hindsight-shipping
description: Getting documents into the platform memory bank — frontmatter contract, the ship path, gates, and gotcha capture
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

## Gates

Approval is conversational; recording it is the archivist's job. When the
founder approves a doc, route `mol-hindsight-gate` to the archivist with
`doc_path`, `verdict`, and `approved_by` (who + traceable venue). Never
flip `status: accepted` + `source: human` yourself on a human's behalf.

Trusted workflows that are explicitly permitted to accept their own specs
set `status: accepted` + `source: agent` — that pair means exactly
"standing position, never human-reviewed."

## Gotcha capture — the one direct-write exception

When something costs you real time and would cost the next agent the same,
write a gotcha doc in your rig's docs tree and commit it; the sync ships
it. Keep it short and literal:

```markdown
---
schema_version: 2
id: gotcha.tflint-provider-cache-lock
type: gotcha
title: tflint provider cache lock
source: agent
scope: repo
repos: [terraform-platform]
created_at: <now>
updated_at: <now>
---
## Symptom
<exact error text>
## Cause
<what was actually wrong>
## Fix
<the correct approach, exact commands>
## Applies to
<where this bites>
```

Do NOT harvest gotchas from session transcripts, and do not retain
progress notes, tool output, or coordination chatter — the never-retain
list in the contract is absolute.

## If you must call the ship script yourself

Formula final steps may run the sync for immediacy:

```bash
<pack>/assets/scripts/ship-docs.sh --bank stacked-chips-v2 <docs-root>
```

It validates, skips unchanged docs, serializes per document (never races a
pending operation — racing orphans memories permanently), and polls
operations to terminal. Exit non-zero means refused or failed docs; read
the report, fix the doc, never work around the validator. Direct API
retains outside this script are forbidden.
