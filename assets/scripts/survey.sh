#!/usr/bin/env bash
# survey.sh — the deterministic half of the current-state survey formula.
# The surveyor agent spends its judgment on reading code and writing the
# document; everything mechanical (worktree, frontmatter, publish, cleanup)
# is here so it can change without touching a prompt.
#
#   survey.sh prepare <root-bead-id> [--output-dir <dir>] [--publish pr|direct|none] [--pr-tool auto|gh|glab]
#   survey.sh show    <root-bead-id>
#   survey.sh stamp   <root-bead-id>
#   survey.sh publish <root-bead-id> [--body-file <file>]
#   survey.sh cleanup <root-bead-id> [--force]
#
# Subcommands:
#   prepare   Create the survey worktree off the up-to-date remote default
#             branch and record the handoff on the workflow root bead.
#             Prints the resolved worktree path.
#   show      Print the handoff recorded on the root (key=value lines).
#   stamp     Replace or prepend the frontmatter block on the survey
#             document with the pack's current-state contract and validate
#             the result against schemas/docs/derive. Never touches the body.
#   publish   pr:     push the survey branch and open (or reuse) a PR/MR
#             direct: push the survey commit straight to the default branch
#             none:   record that nothing was published
#   cleanup   Remove the worktree and local branch when the run passed and
#             its commit is safely on origin; otherwise preserve it and say
#             why. --force removes regardless (operator use).
#
# Identity comes from the managed session env: GC_RIG, GC_RIG_ROOT,
# GC_CITY. Outside a managed session, run from inside a rig checkout and
# the script resolves them from `gc rig list`.
#
# Handoff state lives on the workflow root bead's metadata:
#   work_dir               absolute worktree path (also protects the
#                          worktree from gc's closed-bead reaper)
#   survey_branch          survey/current-state-<yyyymmdd>
#   survey_default_branch  remote default branch the worktree is based on
#   survey_output_dir      rig-relative output directory
#   survey_doc             rig-relative path of the document
#   survey_publish         pr | direct | none
#   survey_pr_tool         auto | gh | glab
#   survey_publish_status  set by publish: pr | pushed | none
#   survey_pr_url          set by publish in pr mode
#   survey_cleanup         set by cleanup: removed | preserved: <reason>
#
# Exit codes:
#   0  ok
#   2  usage / missing identity / missing handoff
#   3  frontmatter refused by the schema (stamp)
#   4  git or PR-tool failure
set -euo pipefail

GC="${GC_BIN:-gc}"
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DERIVE="$PACK_DIR/schemas/docs/derive"

die() { echo "survey: $*" >&2; exit "${EXIT:-2}"; }
gitdie() { EXIT=4; die "$@"; }
info() { echo "survey: $*" >&2; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

# --- identity --------------------------------------------------------------

resolve_identity() {
  if [[ -z "${GC_RIG:-}" || -z "${GC_RIG_ROOT:-}" ]]; then
    # Human running by hand: find the rig whose path contains the cwd.
    local here; here="$(pwd -P)"
    local line
    while IFS=$'\t' read -r name path; do
      [[ -n "$path" ]] || continue
      case "$here/" in "$path"/*) GC_RIG="$name"; GC_RIG_ROOT="$path"; break ;; esac
    done < <("$GC" rig list --json 2>/dev/null | jq -r '.rigs[]? | [.name, .path] | @tsv' 2>/dev/null || true)
    [[ -n "${GC_RIG:-}" && -n "${GC_RIG_ROOT:-}" ]] \
      || die "no rig identity: run inside a managed rig session (GC_RIG/GC_RIG_ROOT) or from inside a rig checkout"
    export GC_RIG GC_RIG_ROOT
  fi
  if [[ -z "${GC_CITY:-}" ]]; then
    GC_CITY="$("$GC" rig list --json 2>/dev/null | jq -r '.city_path // empty' 2>/dev/null || true)"
    [[ -n "$GC_CITY" && -d "$GC_CITY" ]] || die "no city root: GC_CITY is unset and could not be derived"
    export GC_CITY
  fi
  [[ -d "$GC_RIG_ROOT/.git" || -f "$GC_RIG_ROOT/.git" ]] || die "rig root $GC_RIG_ROOT is not a git checkout"
}

# --- bead helpers ----------------------------------------------------------

bead_json() { "$GC" bd show "$1" --json | jq -c 'if type=="array" then .[0] else . end'; }
bead_meta() { bead_json "$1" | jq -r --arg k "$2" '.metadata[$k] // empty'; }
bead_set()  { local id="$1"; shift; "$GC" bd update "$id" "${@/#/--set-metadata=}" >/dev/null; }

require_root() {
  ROOT="${1:-}"
  [[ -n "$ROOT" ]] || { usage >&2; exit 2; }
  bead_json "$ROOT" >/dev/null 2>&1 || die "root bead $ROOT not found"
}

load_handoff() {
  WORKTREE="$(bead_meta "$ROOT" work_dir)"
  BRANCH="$(bead_meta "$ROOT" survey_branch)"
  DEFAULT_BRANCH="$(bead_meta "$ROOT" survey_default_branch)"
  OUTPUT_DIR="$(bead_meta "$ROOT" survey_output_dir)"
  DOC="$(bead_meta "$ROOT" survey_doc)"
  PUBLISH="$(bead_meta "$ROOT" survey_publish)"
  PR_TOOL="$(bead_meta "$ROOT" survey_pr_tool)"
  [[ -n "$WORKTREE" && -n "$BRANCH" && -n "$DEFAULT_BRANCH" && -n "$DOC" && -n "$PUBLISH" ]] \
    || die "root $ROOT has no survey handoff — run 'prepare' first"
  [[ -d "$WORKTREE" ]] || die "worktree $WORKTREE recorded on $ROOT does not exist"
  [[ "$WORKTREE" != "$(cd "$GC_RIG_ROOT" && pwd -P)" ]] || die "work_dir on $ROOT is the rig root; refusing"
}

# --- git helpers -----------------------------------------------------------

rig_git() { git -C "$GC_RIG_ROOT" "$@"; }
wt_git()  { git -C "$WORKTREE" "$@"; }

resolve_default_branch() {
  local b
  b="$(rig_git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)"
  if [[ -z "$b" ]]; then
    rig_git remote set-head origin --auto >/dev/null 2>&1 || true
    b="$(rig_git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)"
  fi
  [[ -n "$b" ]] || die "cannot resolve origin's default branch (refs/remotes/origin/HEAD); fix the rig checkout"
  echo "$b"
}

is_registered_worktree() {
  # Compare common dirs rather than paths: macOS symlinks (/var vs
  # /private/var) make textual comparison against `worktree list` unreliable.
  local mine theirs
  mine="$(rig_git rev-parse --path-format=absolute --git-common-dir)"
  theirs="$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [[ -n "$theirs" && "$(cd "$mine" && pwd -P)" == "$(cd "$theirs" && pwd -P)" ]]
}

# --- prepare ---------------------------------------------------------------

cmd_prepare() {
  require_root "${1:-}"; shift || true
  local output_dir="docs/current-state" publish="pr" pr_tool="auto"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output-dir) output_dir="${2%/}"; shift 2 ;;
      --publish) publish="$2"; shift 2 ;;
      --pr-tool) pr_tool="$2"; shift 2 ;;
      *) die "prepare: unknown arg $1" ;;
    esac
  done
  case "$publish" in pr|direct|none) ;; *) die "--publish must be pr, direct or none (got '$publish')" ;; esac
  case "$pr_tool" in auto|gh|glab) ;; *) die "--pr-tool must be auto, gh or glab (got '$pr_tool')" ;; esac
  [[ "$output_dir" != /* && "$output_dir" != *..* ]] || die "--output-dir must be rig-relative (got '$output_dir')"
  resolve_identity

  DEFAULT_BRANCH="$(resolve_default_branch)"
  rig_git fetch --prune origin "$DEFAULT_BRANCH" || gitdie "fetch of origin/$DEFAULT_BRANCH failed"

  WORKTREE="$GC_CITY/.gc/worktrees/$GC_RIG/current-state-$ROOT"
  BRANCH="survey/current-state-$(date -u +%Y%m%d)"

  if [[ -e "$WORKTREE" ]]; then
    is_registered_worktree "$WORKTREE" \
      || die "$WORKTREE exists but is not a worktree of this rig; remove it by hand"
    info "reusing existing worktree $WORKTREE"
    wt_git checkout -q -B "$BRANCH" "origin/$DEFAULT_BRANCH" || gitdie "could not reset $BRANCH in existing worktree"
  else
    mkdir -p "$(dirname "$WORKTREE")"
    if ! rig_git worktree add -B "$BRANCH" "$WORKTREE" "origin/$DEFAULT_BRANCH" >/dev/null; then
      local holder
      holder="$(rig_git worktree list --porcelain | awk -v b="refs/heads/$BRANCH" '$1=="worktree"{w=$2} $1=="branch"&&$2==b{print w}')"
      gitdie "git worktree add failed${holder:+ — branch $BRANCH is checked out in $holder (a preserved earlier run?)}"
    fi
  fi
  mkdir -p "$WORKTREE/$output_dir"

  bead_set "$ROOT" \
    "work_dir=$WORKTREE" \
    "survey_branch=$BRANCH" \
    "survey_default_branch=$DEFAULT_BRANCH" \
    "survey_output_dir=$output_dir" \
    "survey_doc=$output_dir/README.md" \
    "survey_publish=$publish" \
    "survey_pr_tool=$pr_tool"

  # Fail loudly if the handoff did not land: every later step depends on it.
  [[ "$(bead_meta "$ROOT" work_dir)" == "$WORKTREE" ]] || die "work_dir did not persist on $ROOT"
  cmd_show "$ROOT"
}

# --- show ------------------------------------------------------------------

cmd_show() {
  require_root "${1:-}"
  bead_json "$ROOT" | jq -r '.metadata | to_entries[] | select(.key=="work_dir" or (.key|startswith("survey_"))) | "\(.key)=\(.value)"'
}

# --- stamp -----------------------------------------------------------------

frontmatter() {
  local source="agent"
  [[ "$PUBLISH" == "pr" ]] && source="human"
  cat <<EOF
---
schema_version: 2
id: current-state.$GC_RIG
type: current-state
title: $GC_RIG Current State
source: $source
scope: repo
repos: [$GC_RIG]
updated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
---
EOF
}

cmd_stamp() {
  require_root "${1:-}"
  resolve_identity; load_handoff
  local doc="$WORKTREE/$DOC"
  [[ -s "$doc" ]] || die "survey document $doc is missing or empty — write the body first"
  [[ -x "$DERIVE" ]] || die "schema derive $DERIVE is not executable"

  local tmp; tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
  frontmatter > "$tmp"
  # Drop an existing leading frontmatter block (--- ... ---) so re-stamping is
  # idempotent; the body is appended untouched.
  awk 'NR==1 && $0 ~ /^---[[:space:]]*$/ {skip=1; next}
       skip && $0 ~ /^---[[:space:]]*$/ {skip=0; next}
       !skip' "$doc" >> "$tmp"

  local verdict
  verdict="$("$DERIVE" < "$tmp")"
  case "$(jq -r .verdict <<<"$verdict")" in
    ship) ;;
    *) echo "$verdict" >&2; EXIT=3 die "schema refused the stamped document; body left untouched" ;;
  esac
  mv -f "$tmp" "$doc"; trap - EXIT
  info "stamped $doc"
  sed -n '1,/^---[[:space:]]*$/p' "$doc" | sed '1d;$d'
}

# --- publish ---------------------------------------------------------------

detect_pr_tool() {
  local url host
  url="$(rig_git remote get-url origin)"
  host="$(sed -E 's#^[a-z+]+://([^@/]+@)?##; s#^[^@]+@##; s#[:/].*$##' <<<"$url")"
  case "$host" in
    github.com|*.github.com) echo gh ;;
    gitlab.com|*gitlab*) echo glab ;;
    *) die "cannot infer the PR tool from origin host '$host'; rerun prepare with --pr-tool gh|glab" ;;
  esac
}

pr_body() {
  local body_file="$1"
  if [[ -n "$body_file" ]]; then cat "$body_file"; return; fi
  cat <<EOF
Regenerated \`$DOC\` — the current-state survey of \`$GC_RIG\`, written from the code as of \`origin/$DEFAULT_BRANCH\` at $(wt_git rev-parse --short "origin/$DEFAULT_BRANCH").

Merging replaces the previous survey in the Hindsight bank (\`current-state.$GC_RIG\`).

Workflow root: \`$ROOT\`

\`\`\`
$(wt_git diff --stat "origin/$DEFAULT_BRANCH" -- "$DOC")
\`\`\`
EOF
}

cmd_publish() {
  require_root "${1:-}"; shift || true
  local body_file=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --body-file) body_file="$2"; shift 2 ;;
      *) die "publish: unknown arg $1" ;;
    esac
  done
  resolve_identity; load_handoff
  [[ -z "$(wt_git status --porcelain)" ]] || die "worktree $WORKTREE has uncommitted changes; commit the survey first"
  wt_git diff --quiet "origin/$DEFAULT_BRANCH" -- "$DOC" && die "$DOC has no committed changes against origin/$DEFAULT_BRANCH; nothing to publish"

  local title="docs: current-state survey of $GC_RIG ($(date -u +%Y-%m-%d))"
  case "$PUBLISH" in
    none)
      bead_set "$ROOT" "survey_publish_status=none"
      info "publish=none: branch $BRANCH left in $WORKTREE, nothing pushed"
      ;;
    direct)
      if ! wt_git push origin "HEAD:refs/heads/$DEFAULT_BRANCH" 2>&1; then
        info "push rejected; rebasing onto origin/$DEFAULT_BRANCH once"
        wt_git fetch origin "$DEFAULT_BRANCH" || gitdie "fetch failed"
        wt_git rebase "origin/$DEFAULT_BRANCH" || { wt_git rebase --abort || true; gitdie "rebase failed; resolve by hand"; }
        wt_git push origin "HEAD:refs/heads/$DEFAULT_BRANCH" || gitdie "push to $DEFAULT_BRANCH failed after rebase"
      fi
      bead_set "$ROOT" "survey_publish_status=pushed"
      info "pushed $(wt_git rev-parse --short HEAD) to origin/$DEFAULT_BRANCH"
      ;;
    pr)
      wt_git push --force-with-lease -u origin "$BRANCH" || gitdie "push of $BRANCH failed"
      local tool="$PR_TOOL"; [[ "$tool" == "auto" ]] && tool="$(detect_pr_tool)"
      command -v "$tool" >/dev/null || die "PR tool '$tool' is not installed"
      local url=""
      case "$tool" in
        gh)
          url="$(cd "$WORKTREE" && gh pr list --head "$BRANCH" --state open --json url --jq '.[0].url // empty' 2>/dev/null || true)"
          if [[ -z "$url" ]]; then
            url="$(cd "$WORKTREE" && pr_body "$body_file" | gh pr create --base "$DEFAULT_BRANCH" --head "$BRANCH" --title "$title" --body-file -)" \
              || gitdie "gh pr create failed"
          fi
          ;;
        glab)
          url="$(cd "$WORKTREE" && glab mr list --source-branch "$BRANCH" --state opened -F json 2>/dev/null | jq -r '.[0].web_url // empty' || true)"
          if [[ -z "$url" ]]; then
            url="$(cd "$WORKTREE" && glab mr create --source-branch "$BRANCH" --target-branch "$DEFAULT_BRANCH" --title "$title" --description "$(pr_body "$body_file")" --yes 2>&1 | grep -Eo 'https?://[^ ]+' | tail -1)" \
              || gitdie "glab mr create failed"
          fi
          ;;
      esac
      [[ -n "$url" ]] || gitdie "$tool did not return a PR URL"
      bead_set "$ROOT" "survey_publish_status=pr" "survey_pr_url=$url"
      echo "$url"
      ;;
  esac
}

# --- cleanup ---------------------------------------------------------------

preserve() {
  bead_set "$ROOT" "survey_cleanup=preserved: $1"
  info "preserved $WORKTREE (branch $BRANCH): $1"
  exit 0
}

cmd_cleanup() {
  require_root "${1:-}"; shift || true
  local force=false
  [[ "${1:-}" == "--force" ]] && force=true
  resolve_identity
  # Teardown runs even when prepare never got as far as recording a
  # worktree; that is "nothing to clean", not an error.
  if [[ -z "$(bead_meta "$ROOT" work_dir)" ]]; then
    bead_set "$ROOT" "survey_cleanup=nothing to clean (no work_dir recorded)"
    info "no worktree recorded on $ROOT; nothing to clean"
    exit 0
  fi
  load_handoff

  if ! $force; then
    local outcome status
    outcome="$(bead_meta "$ROOT" gc.outcome)"
    status="$(bead_meta "$ROOT" survey_publish_status)"
    [[ "$outcome" == "pass" ]] || preserve "run outcome is '${outcome:-unset}', not pass"
    [[ "$PUBLISH" != "none" ]] || preserve "publish=none keeps the branch for inspection"
    [[ -z "$(wt_git status --porcelain)" ]] || preserve "worktree has uncommitted changes"
    local head; head="$(wt_git rev-parse HEAD)"
    case "$status" in
      pr)
        wt_git fetch -q origin "$BRANCH" || preserve "could not fetch origin/$BRANCH"
        [[ "$(wt_git rev-parse "origin/$BRANCH")" == "$head" ]] || preserve "HEAD is not on origin/$BRANCH"
        ;;
      pushed)
        wt_git fetch -q origin "$DEFAULT_BRANCH" || preserve "could not fetch origin/$DEFAULT_BRANCH"
        wt_git merge-base --is-ancestor "$head" "origin/$DEFAULT_BRANCH" || preserve "HEAD is not on origin/$DEFAULT_BRANCH"
        ;;
      *) preserve "publish never ran (status '${status:-unset}')" ;;
    esac
  fi

  rig_git worktree remove --force "$WORKTREE" || gitdie "git worktree remove failed"
  rig_git branch -D "$BRANCH" >/dev/null 2>&1 || true
  rig_git worktree prune
  bead_set "$ROOT" "survey_cleanup=removed"
  info "removed $WORKTREE and local branch $BRANCH"
}

# --- main ------------------------------------------------------------------

sub="${1:-}"; shift || true
case "$sub" in
  prepare) cmd_prepare "$@" ;;
  show)    cmd_show "$@" ;;
  stamp)   cmd_stamp "$@" ;;
  publish) cmd_publish "$@" ;;
  cleanup) cmd_cleanup "$@" ;;
  -h|--help|"") usage; [[ -n "$sub" ]] && exit 0 || exit 2 ;;
  *) die "unknown subcommand '$sub'" ;;
esac
