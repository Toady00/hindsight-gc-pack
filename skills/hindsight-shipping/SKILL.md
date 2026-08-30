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
and retains the memory if it clears the bar. One filter, one writer.

Do NOT harvest proposals from session transcripts, and never propose
progress notes, tool output, or coordination chatter — the never-retain
list in the contract is absolute.

### Archivist mechanics (arbitration accepted → retain)

Agent memories are **bank-native** — no file, no commit, no git. Like
voice memos, they live only in the bank; the docs corpus ships from
git, agent memories ship from arbitration. The single write path is
`assets/scripts/memory-retain.sh`, which builds the contract payload,
refuses to race a pending operation for the same `document_id` (racing
orphans memories permanently), and polls to terminal:

```bash
# Accept a new memory (content on stdin, Symptom/Cause/Fix/Applies-to):
memory-retain.sh --id gotcha.tflint-provider-cache-lock \
  --title "tflint provider cache lock" \
  --repos terraform-platform --scope repo << 'EOF'
## Symptom
<exact error text>
## Cause
<what was actually wrong>
## Fix
<the correct approach, exact commands>
## Applies to
<where this bites>
EOF

# Repeat report, nothing new: bump count + refresh timestamp
memory-retain.sh --bump --id gotcha.tflint-provider-cache-lock

# Repeat report with new information: merge, then bump with the merged content
memory-retain.sh --bump --id gotcha.tflint-provider-cache-lock --content-file merged.md
```

Usually `type: gotcha`; pick the type that fits — the type table is the
menu, and new agent-memory types are added to the schema deliberately,
not improvised mid-arbitration. Mint ids as `<type>.<slug>`, stable and
unique; tag `repos:` for every rig the memory bites (that is how rig
agents discover it — retrieval rides tags, not residency).

The script appends a `Reported N times (last: ...)` line to the
content on every retain — body text on purpose, so agents whose
reflect surfaces the memory also see how often it bites — and stamps
`metadata: {kind: "agent-memory", hit_count: N}`. Every bump re-retains
the same `document_id`, which refreshes the bank timestamp: a
frequently-hit issue stays fresh, and one whose truth has drifted ages
out of recency naturally.

**Dedup check** (before judging any proposal):

```bash
TMP=$(mktemp -d)
# semantic match against the bank, scoped to the reported rig
hindsight -o json memory recall "$HINDSIGHT_BANK" "<symptom>" \
  --tags repo:<rig>,scope:platform --tags-match any_strict \
  --budget low --max-tokens 1024 > "$TMP/dedup.json" 2>/dev/null
jq -r '.results[]?.text' "$TMP/dedup.json"
# existing agent memories, with their ids and hit counts
hindsight -o json document list "$HINDSIGHT_BANK" > "$TMP/docs.json" 2>/dev/null
jq -r '.[] | select(.document_metadata.kind == "agent-memory")
  | "\(.id)\thits=\(.document_metadata.hit_count // 1)"' "$TMP/docs.json"
```

A rising `hit_count` on an existing memory means the memory is not
landing — a system-deficiency signal (injection or retrieval is
failing those agents), surfaced by the same documents-list query.

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
