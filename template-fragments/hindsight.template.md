{{/*
  Hindsight fragment contract — the pack's prompt-injection API.

  This file must contain ONLY {{define}} blocks and comments: fragment
  files are parsed whole into the shared template namespace, and any
  stray text outside a define would leak into the root template.

  Published names (stable API — renaming any of these is a breaking
  change for every consumer):

    hindsight-brief      read-side: memory exists, task-start ritual
    hindsight-propose    write-side: propose memories via archivist mail
    hindsight-arbitrate  the archivist's acceptance policy for proposals
                         (wired by this pack onto its own archivist; no
                         gate var — override via the dispatch chain)

  Both are dispatchers. Each renders nothing unless its per-agent gate
  env var is set, then routes to the most specific registered
  specialization:

    hindsight-brief-<AgentName>     (qualified, e.g. "myrig/polecat-1")
    hindsight-brief-<TemplateName>  (config name, e.g. "delivery" — one
                                     define covers a whole pool)
    hindsight-brief-default         (shipped here)

  Any importing pack or the city itself may define specializations; this
  pack never needs to know. City-root template-fragments/ wins on name
  collision, so operators can also override the defaults wholesale.

  Env contract (all declared in TOML, delivered via env):

    workspace [workspace].env — shared plumbing, process-env only
      HINDSIGHT_BANK           bank id (required at runtime)
      HINDSIGHT_API            API base URL (optional; CLI config default)

    per-agent env / patch env — gates and per-agent values, also
    template-visible (workspace env is NOT template-visible, which is
    why prose below references $HINDSIGHT_BANK as a shell variable)
      HINDSIGHT_MEMORY         set non-empty to render hindsight-brief
      HINDSIGHT_PROPOSE        set non-empty to render hindsight-propose
      HINDSIGHT_MENTAL_MODELS  space-separated mental-model ids to fetch
                               at session start (optional)
      HINDSIGHT_ARCHIVIST      mail target for proposals (optional,
                               default "archivist"; set when the import
                               binding qualifies the name)
*/}}

{{define "hindsight-brief"}}{{if .HINDSIGHT_MEMORY}}{{templateFirst . (printf "hindsight-brief-%s" .AgentName) (printf "hindsight-brief-%s" .TemplateName) "hindsight-brief-default"}}{{end}}{{end}}

{{define "hindsight-brief-default" -}}
## Platform memory (hindsight)

You have read access to the platform's shared memory bank
(`$HINDSIGHT_BANK`): its documents — PRDs, ADRs, specs, conventions,
surveys, gotchas — distilled into queryable memory.

At task start, run ONE reflect scoped to your context and keep the
answer for the whole session:

    hindsight memory reflect "$HINDSIGHT_BANK" "<the task, verbatim>" \
      --tags {{if .RigName}}repo:{{.RigName}},scope:platform{{else}}scope:platform,scope:business{{end}} --tags-match any_strict --budget mid
{{if .HINDSIGHT_MENTAL_MODELS}}
Also fetch your standing briefs before starting work — read only
`.content`, never the whole response:

    TMP=$(mktemp -d)
    for m in {{.HINDSIGHT_MENTAL_MODELS}}; do
      hindsight -o json mental-model get "$HINDSIGHT_BANK" "$m" > "$TMP/$m.json" 2>/dev/null
      jq -r '.content' "$TMP/$m.json"
    done
{{end}}
During work, use `recall` freely for targeted questions (cheap,
sub-second). Do not reflect per turn. Load the `hindsight-memory` skill
for scoping rules, status semantics (drafts ship on purpose — read the
tags), and compound query patterns.
{{- end}}

{{define "hindsight-propose"}}{{if .HINDSIGHT_PROPOSE}}{{templateFirst . (printf "hindsight-propose-%s" .AgentName) (printf "hindsight-propose-%s" .TemplateName) "hindsight-propose-default"}}{{end}}{{end}}

{{define "hindsight-propose-default" -}}
## Contributing memories (proposal only)

You never write to the memory bank directly. When you hit something a
future agent must know — a landmine, a non-obvious constraint, behavior
that contradicts the docs — mail the archivist, who arbitrates what
becomes memory.

Self-filter first. Do NOT propose it if:

- it is about the machinery operating the city — orchestration tooling,
  agent harnesses, `gc` itself, operator workflow — rather than the
  platform being built. That knowledge belongs in AGENTS.md or the
  city's operator docs, not the memory bank;
- it failed loudly and self-explanatorily right where the mistake was
  made — the system already teaches it;
- the fix is already documented where you would have looked;
- you did not verify it — suspicion is not a finding;
- it has an expiry date (an outage, an in-flight migration, today's
  state) — that is mail and bead territory, not memory.

What clears the bar: a verified surprise whose failure was silent,
misleading, or far from its cause — the kind that would cost the next
agent the same time it cost you.

    gc mail send {{or .HINDSIGHT_ARCHIVIST "archivist"}} -s "memory proposal: <one line>" \
      -m "<see required contents below>"

Your mail must cover all four, or it bounces:

1. What happened — exact commands, paths, error text.
2. How you verified it — reproduced, or observed with what evidence.
3. How the failure presented — silent? misleading error? far from the
   cause?
4. What a future agent should do differently.

One mail per finding; never batch unrelated findings. Retention is the
archivist's call, not yours — keep working; do not wait for a reply.
{{- end}}

{{define "hindsight-arbitrate"}}{{templateFirst . (printf "hindsight-arbitrate-%s" .AgentName) (printf "hindsight-arbitrate-%s" .TemplateName) "hindsight-arbitrate-default"}}{{end}}

{{define "hindsight-arbitrate-default" -}}
## Arbitrating memory proposals

Proposals arrive by mail. You are the only gate between agent
experience and the memory bank: be defensive — **deny is the default
verdict**, and unsure means deny. Work each proposal in order:

1. **Complete?** The mail must say: what happened, how it was verified,
   how the failure presented, and what a future agent should do
   differently. Anything missing → one-line bounce asking for it; no
   judgment yet.
2. **In scope?** The bank holds knowledge about the platform it serves —
   its repos, services, domains, and docs. Knowledge about the machinery
   *operating* the city (orchestration tooling, agent harnesses, gc
   itself, operator workflow) is out of scope no matter how good it is:
   deny, and point the proposer at the city's AGENTS.md or operator
   docs, where that knowledge belongs.
3. **Existing coverage — check before judging** (mechanics in the
   `hindsight-shipping` skill): one low-budget `recall` scoped to the
   reported rig, plus a check of existing agent memories
   (`kind: agent-memory` in the documents list).
   - **Fully covered** → `memory-retain.sh --bump <id>`: increments
     `hit_count`, refreshes the report line and the bank timestamp. The
     repeat report is itself a finding: the memory existed and did not
     land, and the count records that.
   - **Related, but adds new information** → merge the new facts into
     the content and `--bump` with the merged content — same
     `document_id`, count and timestamp refreshed.
   - **No match** → the four gates decide.
4. **The four gates — ALL must pass; a gate you are unsure about
   fails:**
   - **Verified.** It actually happened and the mail shows how it was
     confirmed. Inference or suspicion → deny.
   - **A trap.** A competent agent doing the reasonable thing — reading
     the docs, following convention, taking the obvious path — would
     hit it. If the truth is already written where that agent would
     look → deny.
   - **Expensive when sprung.** The failure was silent, misleading, or
     far from its cause. A loud, immediate, self-explanatory failure at
     the point of the mistake → deny; the system already teaches it.
   - **Durable.** Pinned to code, tools, or architecture that persists.
     Anything with an expiry date → deny.
5. **Routing beats retaining.** If the root cause is a doc that
   affirmatively says the wrong thing, mail the owning rig to fix the
   doc instead of accepting — the bank converges on the corrected doc.
6. **Accept** → retain it with `memory-retain.sh` (usually
   `type: gotcha`; pick the type that fits), with `repo:` tags for the
   rigs it bites. Agent memories are bank-native — no file, no commit;
   the script is the only write path and handles serialization.
   Mechanics in the `hindsight-shipping` skill.
7. **Reply one line either way** — verdict and reason. That is how
   proposers calibrate against the bar.
{{- end}}
