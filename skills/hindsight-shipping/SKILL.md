---
name: hindsight-shipping
description: Getting documents into the platform memory bank — frontmatter contract, the ship path, approval recording, and agent-contributed memory via archivist proposals
---

# Shipping documents to the memory bank

The bank converges on the docs tree: **anything with valid frontmatter
ships, drafts included**. You get content into the bank by writing a doc
and committing it — not by calling the Hindsight API. The archivist's
scheduled sync (`gc hindsight ship`) is the single ship path. The supported
v1 import binding is `hindsight`, regardless of the consuming agent's pack.

The enforcement point is the schema itself (`schemas/docs/derive` —
what it refuses is the contract). This skill is the working reference
for authoring agents under the **docs schema**, the pack's default
dialect; a city shipping a different dialect swaps it via
`gc hindsight ship --schema` and brings its own version of this skill.

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
updated_at: 2026-08-20T15:30:00Z    # revision timestamp; the full-file hash drives re-ship
---
```

Types: `adr` `spec` `hld` `prd` `user-journey` `methodology` `convention`
`current-state` `meeting-notes` `discussion` `voice-memo` `build-report`
`runbook` `gotcha` `external`. Anything else is refused at ship time.
Every type requires `status`, including current-state, build-report, gotcha,
voice-memo, and external. Status never selects extraction strategy; `type` does.
Acceptance admits a record without changing its kind of evidence: a survey
remains an observation and a voice memo remains thinking. Missing status is
refused. Legacy bank-native memories need an explicit `--status` on first bump.

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
  `gc hindsight ship` command, or let the hourly sync catch it).
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
`gc hindsight retain`, backed by `memory-retain.sh`. The helper builds the
contract payload, refuses to race a pending operation for the same `document_id` (racing
orphans memories permanently), and polls to terminal:

```bash
# Accept a new memory (content on stdin, Symptom/Cause/Fix/Applies-to):
gc hindsight retain --id gotcha.tflint-provider-cache-lock \
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
gc hindsight retain --bump --id gotcha.tflint-provider-cache-lock

# Repeat report with new information: merge, then bump with the merged content
gc hindsight retain --bump --id gotcha.tflint-provider-cache-lock --content-file merged.md
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
gc hindsight read -o json memory recall "$HINDSIGHT_BANK" "<symptom>" \
  --tags repo:<rig>,scope:platform --tags-match any_strict \
  --budget low --max-tokens 1024 > "$TMP/dedup.json" 2>/dev/null
jq -r '.results[]?.text' "$TMP/dedup.json"
# existing agent memories, with their ids and hit counts (paged: .items)
gc hindsight read -o json document list "$HINDSIGHT_BANK" > "$TMP/docs.json" 2>/dev/null
jq -r '.items[] | select(.document_metadata.kind == "agent-memory")
  | "\(.id)\thits=\(.document_metadata.hit_count // 1)"' "$TMP/docs.json"
```

A rising `hit_count` on an existing memory means the memory is not
landing — a system-deficiency signal (injection or retrieval is
failing those agents), surfaced by the same documents-list query.

## Requesting an immediate ship

Formula final steps may run the sync for immediacy:

```bash
gc hindsight ship <docs-root>   # queues work to the archivist
```

Outside the managed archivist, this queues a formula and returns after dispatch.
The formula bead carries the eventual result. Within the archivist it validates,
skips unchanged docs, drains prior operations and polls new ones to terminal.
Only the archivist may invoke write scripts directly, and it must run them one
at a time in the foreground. `--dry-run` previews locally. `--reprocess` queues
re-extraction even when content hashes match. Direct API retains are forbidden.
