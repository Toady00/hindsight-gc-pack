{{/*
  Hindsight fragment contract — the pack's prompt-injection API.

  This file must contain ONLY {{define}} blocks and comments: fragment
  files are parsed whole into the shared template namespace, and any
  stray text outside a define would leak into the root template.

  Published names (stable API — renaming any of these is a breaking
  change for every consumer):

    hindsight-brief      read-side: memory exists, task-start ritual
    hindsight-propose    write-side: propose memories via archivist mail

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
becomes memory:

    gc mail send {{or .HINDSIGHT_ARCHIVIST "archivist"}} -s "memory proposal: <one line>" \
      -m "<what you found, where, how you verified it, why a future agent must know>"

One mail per finding; never batch unrelated findings. The archivist may
fold it into a doc, capture it as a gotcha, or reject it — retention is
the archivist's call, not yours. Keep working; do not wait for a reply.
{{- end}}
