---
name: hindsight-memory
description: Reading the platform memory bank — reflect at task start, recall during work, mental models, scoping, and status semantics
---

# Reading the platform memory bank

Bank: `stacked-chips-v2`. Full read-side reference: `agent-runbook.md` in
the hindsight pack. This skill is the working subset.

```bash
BANK=stacked-chips-v2
```

## The cadence

1. **Task start, once:** one `reflect` scoped to your rig. Keep the answer
   for the whole session. Run it again only if the task changes into a
   different problem.
2. **During work, freely:** `recall` for specific lookups (sub-second,
   cheap) and `mental-model get` for standing answers.
3. **Never:** reflect per turn, recall before every message, or
   `--budget high` before a low/mid pass failed.

```bash
# Task start — your rig name IS your repo tag
hindsight memory reflect $BANK "<the task, verbatim>" \
  --tags repo:<your-rig>,scope:platform --tags-match any_strict --budget mid

# Lookup — redirect, then extract; never dump raw output into context
TMP=$(mktemp -d)
hindsight -o json memory recall $BANK "<question>" \
  --tags repo:<your-rig>,scope:platform --tags-match any_strict \
  --budget low --max-tokens 2048 > "$TMP/r.json" 2>/dev/null
jq -r '.results[]? | .text' "$TMP/r.json"
```

**Before writing code in an unfamiliar rig**, fetch
`conventions-and-standards` and `landmines`:

```bash
hindsight -o json mental-model get $BANK conventions-and-standards > "$TMP/m.json" 2>/dev/null
jq -r '.content' "$TMP/m.json"   # take .content, never the whole object
```

Other standing models: `platform-architecture-and-service-map`,
`implementation-reality-and-intent-drift`, `convention-deviations`,
`analysis-generation-methodology`, `open-risks-and-questions`,
`company-strategy-and-operating-model`.

## Status semantics — everything ships, including drafts

The bank deliberately contains work in flight. Read the tags:

| Pair | Means |
|---|---|
| `status:draft` + `source:agent` | unreviewed agent proposal — direction, not commitment |
| `status:draft` + `source:human` | a person's own unfinished thinking |
| `status:accepted` + `source:human` | ratified by a person |
| `status:accepted` + `source:agent` | standing position from a trusted workflow, never human-reviewed |
| `status:superseded` / `deprecated` | history, kept queryable on purpose |
| no `status:` | voice memos, gotchas, surveys, build reports — status does not apply |

Treat drafts as "which way things are leaning" — useful for tangential
work, never citable as platform fact.

## Scoping

`any_strict` = match ANY listed tag, excluding untagged. This is the OR
that makes platform-wide docs visible from inside a rig. Never use plain
`any` (it matches untagged memories, not "memories missing one tag").

| Reach | Flags |
|---|---|
| Your rig + platform | `--tags repo:<rig>,scope:platform --tags-match any_strict` |
| + business strategy | add `scope:business` to the list |
| One domain, anywhere | `--tags domain:<d> --tags-match any_strict` |
| Everything | omit `--tags` |

## Hard ground truth: `tag_groups` (API-only)

"(my rig OR platform) AND accepted" cannot be expressed as a flat list —
adding `status:accepted` to an OR list *broadens* it. Use the API:

```bash
curl -sS -X POST "$HINDSIGHT_API/v1/default/banks/$BANK/memories/recall" \
  -H 'Content-Type: application/json' -d '{
  "query": "<question>",
  "tag_groups": [
    {"tags": ["repo:<rig>", "scope:platform"], "match": "any_strict"},
    {"tags": ["status:accepted"], "match": "any_strict"}
  ],
  "max_tokens": 2048}'
```

Verified caveats:

- Requiring `status:accepted` is airtight but returns **raw memories
  only** — observations (the consolidated layer) carry only their
  observation-scope tag, never status/source/memory_type.
- A `{"not": ...}` on status is a **soft filter**: status-less
  observations and memories pass through. Never use NOT for
  compliance-grade exclusion; use the positive form.

## What the bank is NOT

- Not work-state. What is happening lives in beads/convoys/mail.
- Not a substitute for reading your own rig's code — inside your rig, git
  is truth. The bank's surveys are for cross-repo visibility.
- Not writable by you. Doc changes reach the bank by committing docs with
  valid frontmatter (the archivist's sync ships them). Exception: gotcha
  capture — see the `hindsight-shipping` skill.
