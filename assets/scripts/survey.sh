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
#   cleanup   Remove the worktree and local branch when its scope passed and
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
#   survey_branch          survey/current-state-<root-bead-id>
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
  local rigs rows row here name path matches=0 selected_name="" selected_path=""
  if [[ -z "${GC_RIG:-}" || -z "${GC_RIG_ROOT:-}" || -z "${GC_CITY:-}" ]]; then
    rigs="$("$GC" rig list --json)" || die "could not list rigs"
    jq -e 'type == "object" and (.rigs | type == "array")
      and (.city_path | type == "string" and startswith("/"))' <<<"$rigs" >/dev/null || die "invalid rig list response"
    path="$(jq -r '.city_path' <<<"$rigs")"
    path="$(cd "$path" && pwd -P)" || die "could not resolve listed city path"
    if [[ -n "${GC_CITY:-}" ]]; then
      [[ "$(cd "$GC_CITY" && pwd -P)" == "$path" ]] || die "listed city conflicts with GC_CITY"
    fi
    GC_CITY="$path"
  fi
  if [[ -z "${GC_RIG:-}" || -z "${GC_RIG_ROOT:-}" ]]; then
    here="$(pwd -P)"
    rows="$(jq -ce '.rigs[] | select(.hq != true)' <<<"$rigs")" || die "no non-HQ rigs found"
    while IFS= read -r row; do
      name="$(jq -er '.name | select(type == "string" and test("^[a-zA-Z0-9][a-zA-Z0-9._-]*$"))' <<<"$row")" || die "invalid listed rig name"
      path="$(jq -er '.path | select(type == "string" and startswith("/"))' <<<"$row")" || die "invalid listed rig path"
      path="$(cd "$path" && pwd -P)" || die "could not resolve listed rig path"
      case "$here/" in "$path"/*)
        matches=$((matches + 1)); selected_name="$name"; selected_path="$path" ;;
      esac
    done <<<"$rows"
    [[ "$matches" == 1 ]] || die "expected one containing non-HQ rig, found $matches"
    [[ -z "${GC_RIG:-}" || "$GC_RIG" == "$selected_name" ]] || die "listed rig conflicts with GC_RIG"
    if [[ -n "${GC_RIG_ROOT:-}" ]]; then
      [[ "$(cd "$GC_RIG_ROOT" && pwd -P)" == "$selected_path" ]] || die "listed rig conflicts with GC_RIG_ROOT"
    fi
    GC_RIG="$selected_name"; GC_RIG_ROOT="$selected_path"
  fi
  [[ -d "$GC_RIG_ROOT/.git" || -f "$GC_RIG_ROOT/.git" ]] || die "rig root $GC_RIG_ROOT is not a git checkout"
  GC_RIG_ROOT="$(cd "$GC_RIG_ROOT" && pwd -P)"
  GC_CITY="$(cd "$GC_CITY" && pwd -P)"
  [[ "$GC_RIG" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || die "unsafe rig name"
  export GC_RIG GC_RIG_ROOT GC_CITY
}

# --- bead helpers ----------------------------------------------------------

# Survey workflows belong to the rig store, including calls from a worktree.
bead_json() { "$GC" bd --rig "$GC_RIG" show "$1" --json | jq -ce --arg id "$1" 'if type=="array" then (if length==1 then .[0] else null end) else . end | select(type=="object" and .id==$id and (.metadata | type=="object" or .==null))'; }
bead_meta() { bead_json "$1" | jq -r --arg k "$2" '.metadata[$k] // empty'; }
bead_set() {
  local id="$1" item saved; shift
  "$GC" bd --rig "$GC_RIG" update "$id" "${@/#/--set-metadata=}" >/dev/null || die "metadata update failed for $id"
  saved="$(bead_json "$id")" || die "metadata readback failed for $id"
  for item in "$@"; do
    jq -e --arg k "${item%%=*}" --arg v "${item#*=}" '.metadata[$k] == $v' <<<"$saved" >/dev/null \
      || die "metadata ${item%%=*} did not persist on $id"
  done
}

require_root() {
  ROOT="${1:-}"
  [[ -n "$ROOT" ]] || { usage >&2; exit 2; }
  [[ "$ROOT" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]] || die "unsafe root bead id"
  local saved
  saved="$(bead_json "$ROOT")" || die "root bead $ROOT not found"
  jq -e --arg store "rig:$GC_RIG" --arg root "$ROOT" '
    .metadata["gc.root_store_ref"] == $store
    and .metadata["gc.kind"] == "workflow"
    and .metadata["gc.formula_name"] == "current-state-survey"
    and ((.metadata["gc.root_bead_id"] // $root) == $root)
    and ((.metadata["gc.step_ref"] // "") == "")' <<<"$saved" >/dev/null \
    || die "$ROOT is not a current-state-survey workflow root in rig:$GC_RIG"
}

load_handoff() {
  local saved
  saved="$(bead_json "$ROOT")"
  WORKTREE="$(jq -r '.metadata.work_dir // empty' <<<"$saved")"
  BRANCH="$(jq -r '.metadata.survey_branch // empty' <<<"$saved")"
  DEFAULT_BRANCH="$(jq -r '.metadata.survey_default_branch // empty' <<<"$saved")"
  OUTPUT_DIR="$(jq -r '.metadata.survey_output_dir // empty' <<<"$saved")"
  DOC="$(jq -r '.metadata.survey_doc // empty' <<<"$saved")"
  PUBLISH="$(jq -r '.metadata.survey_publish // empty' <<<"$saved")"
  PR_TOOL="$(jq -r '.metadata.survey_pr_tool // empty' <<<"$saved")"
  BASE="$(jq -r '.metadata.survey_base // empty' <<<"$saved")"
  [[ -n "$WORKTREE" && -n "$BRANCH" && -n "$DEFAULT_BRANCH" && -n "$DOC" && -n "$PUBLISH" ]] \
    || die "root $ROOT has no survey handoff — run 'prepare' first"
  [[ "$WORKTREE" == "$GC_CITY/.gc/worktrees/$GC_RIG/current-state-$ROOT" && "$BRANCH" == "survey/current-state-$ROOT" ]] || die "handoff path or branch does not belong to $ROOT"
  [[ "$(jq -r '.metadata.survey_rig_root' <<<"$saved")" == "$GC_RIG_ROOT" ]] || die "handoff belongs to another rig"
  [[ "$DOC" == "$OUTPUT_DIR/README.md" ]] || die "handoff document does not match output directory"
  case "$PUBLISH" in pr|direct|none) ;; *) die "invalid handoff publish mode" ;; esac
  case "$PR_TOOL" in auto|gh|glab) ;; *) die "invalid handoff PR tool" ;; esac
  git check-ref-format "refs/heads/$DEFAULT_BRANCH" >/dev/null || die "invalid default branch"
  [[ "$BASE" =~ ^[0-9a-f]{40,64}$ ]] && rig_git cat-file -e "$BASE^{commit}" || die "invalid handoff base"
  safe_path "$GC_CITY" ".gc/worktrees/$GC_RIG/current-state-$ROOT"
  safe_path "$WORKTREE" "$DOC"
  if [[ -e "$WORKTREE" ]]; then
    is_registered_worktree "$WORKTREE" || die "worktree is not owned by this rig"
    [[ "$(wt_git rev-parse --show-toplevel)" == "$WORKTREE" ]] || die "handoff is not a worktree root"
    [[ "$(wt_git symbolic-ref --short HEAD)" == "$BRANCH" ]] || die "worktree is on another branch"
  elif [[ "${1:-}" != allow-missing ]]; then
    die "worktree $WORKTREE recorded on $ROOT does not exist"
  fi
}

# Reject symlinks even when they currently point inside the worktree: a later
# replacement must not redirect a stamp or cleanup into another checkout.
safe_path() {
  local path="$1" relative="$2" part
  [[ -n "$relative" && "$relative" != /* && "$relative" != *$'\n'* && "$relative" != *$'\r'* ]] || die "unsafe relative path"
  local -a parts
  IFS=/ read -r -a parts <<<"$relative"
  for part in "${parts[@]}"; do
    [[ -n "$part" && "$part" != . && "$part" != .. && "$part" != .git ]] || die "unsafe path component"
    path="$path/$part"
    [[ ! -L "$path" ]] || die "symlink in survey path: $path"
  done
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
  local output_given=false publish_given=false tool_given=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output-dir) [[ $# -ge 2 ]] || die "missing output directory"; output_dir="${2%/}"; output_given=true; shift 2 ;;
      --publish) [[ $# -ge 2 ]] || die "missing publish mode"; publish="$2"; publish_given=true; shift 2 ;;
      --pr-tool) [[ $# -ge 2 ]] || die "missing PR tool"; pr_tool="$2"; tool_given=true; shift 2 ;;
      *) die "prepare: unknown arg $1" ;;
    esac
  done
  case "$publish" in pr|direct|none) ;; *) die "--publish must be pr, direct or none (got '$publish')" ;; esac
  case "$pr_tool" in auto|gh|glab) ;; *) die "--pr-tool must be auto, gh or glab (got '$pr_tool')" ;; esac
  safe_path "$GC_RIG_ROOT" "$output_dir/README.md"
  if [[ -n "$(bead_meta "$ROOT" survey_branch)" ]]; then
    load_handoff allow-missing
    [[ "$(bead_meta "$ROOT" survey_cleanup)" != removed ]] || die "survey already cleaned up; use a new root"
    if [[ ! -e "$WORKTREE" && -n "$(bead_meta "$ROOT" survey_cleanup_head)" ]]; then
      die "cleanup is incomplete; retry cleanup, not prepare"
    fi
    if { $output_given && [[ "$output_dir" != "$OUTPUT_DIR" ]]; } \
      || { $publish_given && [[ "$publish" != "$PUBLISH" ]]; } \
      || { $tool_given && [[ "$pr_tool" != "$PR_TOOL" ]]; }; then
      die "prepare options conflict with the persisted handoff; use a new root"
    fi
  else
    [[ -z "$(bead_meta "$ROOT" work_dir)" ]] || die "root already has a work_dir without a survey handoff"
    DEFAULT_BRANCH="$(resolve_default_branch)"
    rig_git fetch origin "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH" || gitdie "fetch failed"
    BASE="$(rig_git rev-parse "refs/remotes/origin/$DEFAULT_BRANCH^{commit}")"
    WORKTREE="$GC_CITY/.gc/worktrees/$GC_RIG/current-state-$ROOT"
    BRANCH="survey/current-state-$ROOT"
    safe_path "$GC_CITY" ".gc/worktrees/$GC_RIG/current-state-$ROOT"
    [[ ! -e "$WORKTREE" ]] || die "unowned worktree path already exists"
    if rig_git show-ref --verify --quiet "refs/heads/$BRANCH"; then die "unowned survey branch already exists"; fi
    bead_set "$ROOT" \
    "work_dir=$WORKTREE" \
    "survey_rig_root=$GC_RIG_ROOT" \
    "survey_base=$BASE" \
    "survey_branch=$BRANCH" \
    "survey_default_branch=$DEFAULT_BRANCH" \
    "survey_output_dir=$output_dir" \
    "survey_doc=$output_dir/README.md" \
    "survey_publish=$publish" \
    "survey_pr_tool=$pr_tool"
    load_handoff allow-missing
  fi
  if [[ ! -e "$WORKTREE" ]]; then
    mkdir -p "$(dirname "$WORKTREE")"
    if rig_git show-ref --verify --quiet "refs/heads/$BRANCH"; then
      rig_git worktree add "$WORKTREE" "$BRANCH" || gitdie "could not recover survey worktree"
    else
      rig_git worktree add -b "$BRANCH" "$WORKTREE" "$BASE" || gitdie "could not create survey worktree"
    fi
  fi
  load_handoff
  mkdir -p "$WORKTREE/$OUTPUT_DIR"
  cmd_show "$ROOT"
}

# --- show ------------------------------------------------------------------

cmd_show() {
  require_root "${1:-}"
  bead_json "$ROOT" | jq -r '.metadata | to_entries[] | select(.key=="work_dir" or (.key|startswith("survey_"))) | "\(.key)=\(.value)"'
}

# --- stamp -----------------------------------------------------------------

frontmatter() {
  cat <<EOF
---
schema_version: 2
id: current-state.$GC_RIG
type: current-state
title: $GC_RIG Current State
status: draft
source: agent
scope: repo
repos: [$GC_RIG]
updated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
---
EOF
}

cmd_stamp() {
  require_root "${1:-}"
  load_handoff
  local doc="$WORKTREE/$DOC"
  [[ -s "$doc" ]] || die "survey document $doc is missing or empty — write the body first"
  [[ -x "$DERIVE" ]] || die "schema derive $DERIVE is not executable"

  tmp="$(mktemp "$WORKTREE/$OUTPUT_DIR/.survey.XXXXXX")"; trap 'rm -f "$tmp"' EXIT
  frontmatter > "$tmp"
  # Drop an existing leading frontmatter block (--- ... ---) so re-stamping is
  # idempotent; the body is appended untouched.
  awk 'NR==1 && $0 ~ /^---[[:space:]]*$/ {skip=1; next}
       skip && $0 ~ /^---[[:space:]]*$/ {skip=0; next}
       !skip {print}
       END {if (skip) exit 1}' "$doc" >> "$tmp" || die "unterminated frontmatter; body left untouched"

  local verdict
  verdict="$("$DERIVE" < "$tmp")"
  case "$(jq -r .verdict <<<"$verdict")" in
    ship) ;;
    *) echo "$verdict" >&2; EXIT=3 die "schema refused the stamped document; body left untouched" ;;
  esac
  mv -f "$tmp" "$doc"; trap - EXIT
  info "stamped $doc"
  awk 'NR == 1 {next} /^---[[:space:]]*$/ {exit} {print}' "$doc"
}

# --- publish ---------------------------------------------------------------

detect_pr_tool() {
  local url host
  url="$(rig_git remote get-url origin)"
  host="$(sed -E 's#^[a-z+]+://([^@/]+@)?##; s#^[^@]+@##; s#[:/].*$##' <<<"$url")"
  case "$host" in
    github.com|*.github.com) echo gh ;;
    gitlab.com|*gitlab*) echo glab ;;
    *) die "cannot infer the PR tool from origin host '$host'; prepare a new root with --pr-tool gh|glab" ;;
  esac
}

pr_body() {
  local body_file="$1" base_short diff_stat
  if [[ -n "$body_file" ]]; then cat "$body_file"; return; fi
  base_short="$(wt_git rev-parse --short "$BASE")" || gitdie "could not read survey base"
  diff_stat="$(wt_git diff --stat "$BASE" HEAD -- "$DOC")" || gitdie "could not read survey diff"
  cat <<EOF
Regenerated \`$DOC\` — the current-state survey of \`$GC_RIG\`, written from the recorded base \`$base_short\` of \`origin/$DEFAULT_BRANCH\`.

This document remains an agent-authored draft. Review approval is a separate action.

Workflow root: \`$ROOT\`

\`\`\`
$diff_stat
\`\`\`
EOF
}

cmd_publish() {
  require_root "${1:-}"; shift || true
  local body_file=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --body-file) [[ $# -ge 2 ]] || die "missing body file"; body_file="$2"; shift 2 ;;
      *) die "publish: unknown arg $1" ;;
    esac
  done
  load_handoff
  local dirty verdict changes commit
  dirty="$(wt_git status --porcelain --untracked-files=all)" || gitdie "git status failed"
  [[ -z "$dirty" ]] || die "worktree $WORKTREE has uncommitted changes; commit the survey first"
  [[ -s "$WORKTREE/$DOC" ]] || die "survey document missing or empty"
  verdict="$(wt_git show "HEAD:$DOC" | "$DERIVE")" || die "committed document schema validation failed"
  jq -e --arg id "current-state.$GC_RIG" '.verdict == "ship" and .document_id == $id and (.tags | index("status:draft") != null and index("source:agent") != null and index("memory_type:current-state") != null)' <<<"$verdict" >/dev/null \
    || die "publish requires a schema-valid agent-authored current-state draft"
  wt_git merge-base --is-ancestor "$BASE" HEAD || die "survey no longer descends from its recorded base"
  changes="$(wt_git log --format= --name-only --no-renames "$BASE..HEAD")" || gitdie "could not inspect survey commits"
  while IFS= read -r commit; do
    [[ -z "$commit" || "$commit" == "$DOC" ]] || die "survey commits modify another path: $commit"
  done <<<"$changes"
  changes="$(wt_git rev-list --merges "$BASE..HEAD")" || gitdie "could not inspect survey history"
  [[ -z "$changes" ]] || die "survey contains merge commits"
  changes="$(wt_git diff --name-only "$BASE" HEAD)" || gitdie "could not inspect survey diff"
  [[ "$changes" == "$DOC" ]] || die "survey must change only $DOC"

  local title="docs: current-state survey of $GC_RIG ($(date -u +%Y-%m-%d))"
  case "$PUBLISH" in
    none)
      bead_set "$ROOT" "survey_publish_status=none"
      info "publish=none: branch $BRANCH left in $WORKTREE, nothing pushed"
      ;;
    direct)
      wt_git push origin "HEAD:refs/heads/$DEFAULT_BRANCH" || gitdie "push rejected; work preserved for manual reconciliation"
      bead_set "$ROOT" "survey_publish_status=pushed"
      info "pushed $(wt_git rev-parse --short HEAD) to origin/$DEFAULT_BRANCH"
      ;;
    pr)
      local tool="$PR_TOOL"; [[ "$tool" == "auto" ]] && tool="$(detect_pr_tool)"
      command -v "$tool" >/dev/null || die "PR tool '$tool' is not installed"
      local body
      body="$(pr_body "$body_file")" || gitdie "could not generate PR body"
      wt_git push -u origin "$BRANCH" || gitdie "push of $BRANCH failed"
      local url=""
      case "$tool" in
        gh)
          url="$(cd "$WORKTREE" && gh pr list --head "$BRANCH" --base "$DEFAULT_BRANCH" --state open --json url --jq '.[0].url // empty')" || gitdie "gh pr list failed"
          if [[ -z "$url" ]]; then
            url="$(cd "$WORKTREE" && gh pr create --base "$DEFAULT_BRANCH" --head "$BRANCH" --title "$title" --body-file - <<<"$body")" \
              || gitdie "gh pr create failed"
          fi
          ;;
        glab)
          url="$(cd "$WORKTREE" && glab mr list --source-branch "$BRANCH" --target-branch "$DEFAULT_BRANCH" -F json | jq -r '.[0].web_url // empty')" || gitdie "glab mr list failed"
          if [[ -z "$url" ]]; then
            url="$(cd "$WORKTREE" && glab mr create --source-branch "$BRANCH" --target-branch "$DEFAULT_BRANCH" --title "$title" --description "$body" --yes 2>&1 | grep -Eo 'https?://[^ ]+' | tail -1)" \
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
  if [[ $# -gt 0 ]]; then
    [[ $# -eq 1 && "$1" == --force ]] || die "cleanup: expected only --force"
    force=true
  fi
  # Teardown runs even when prepare never got as far as recording a
  # worktree; that is "nothing to clean", not an error.
  if [[ -z "$(bead_meta "$ROOT" work_dir)" ]]; then
    bead_set "$ROOT" "survey_cleanup=nothing to clean (no work_dir recorded)"
    info "no worktree recorded on $ROOT; nothing to clean"
    exit 0
  fi
  load_handoff allow-missing
  if [[ ! -e "$WORKTREE" ]]; then
    [[ "$(bead_meta "$ROOT" survey_cleanup)" == removed ]] && return 0
    [[ -n "$(bead_meta "$ROOT" survey_cleanup_head)" ]] || die "worktree missing without a cleanup receipt"
    [[ "$(rig_git rev-parse --verify "refs/heads/$BRANCH" 2>/dev/null || true)" == "$(bead_meta "$ROOT" survey_cleanup_head)" ]] \
      || { if rig_git show-ref --verify --quiet "refs/heads/$BRANCH"; then die "branch changed since cleanup"; fi; }
  else
    if ! $force; then
      local scope status
      scope="$("$GC" bd --rig "$GC_RIG" list --parent "$ROOT" --status all --limit 0 --json)" || preserve "could not read worktree scope"
      jq -e --arg root "$ROOT" '
      if type != "array" then false else
        [.[] | select(.metadata["gc.root_bead_id"] == $root
          and .metadata["gc.step_ref"] == "current-state-survey.worktree"
          and .metadata["gc.kind"] == "scope"
          and .metadata["gc.scope_role"] == "body")]
        | length == 1 and .[0].status == "closed" and .[0].metadata["gc.outcome"] == "pass"
      end' <<<"$scope" >/dev/null || preserve "worktree scope is missing, ambiguous, or not closed with pass"
      status="$(bead_meta "$ROOT" survey_publish_status)"
      [[ "$PUBLISH" != "none" ]] || preserve "publish=none keeps the branch for inspection"
      local dirty
      dirty="$(wt_git status --porcelain --untracked-files=all --ignored)" || gitdie "git status failed"
      [[ -z "$dirty" ]] || preserve "worktree has uncommitted or ignored changes"
      local head; head="$(wt_git rev-parse HEAD)"
      case "$status" in
        pr)
          wt_git fetch -q origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || preserve "could not fetch origin/$BRANCH"
          [[ "$(wt_git rev-parse "origin/$BRANCH")" == "$head" ]] || preserve "HEAD is not on origin/$BRANCH"
          ;;
        pushed)
          wt_git fetch -q origin "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH" || preserve "could not fetch origin/$DEFAULT_BRANCH"
          wt_git merge-base --is-ancestor "$head" "origin/$DEFAULT_BRANCH" || preserve "HEAD is not on origin/$DEFAULT_BRANCH"
          ;;
        *) preserve "publish never ran (status '${status:-unset}')" ;;
      esac
    fi

    local cleanup_head
    cleanup_head="$(wt_git rev-parse HEAD)" || gitdie "could not read cleanup HEAD"
    bead_set "$ROOT" "survey_cleanup_head=$cleanup_head"
    if $force; then
      rig_git worktree remove --force "$WORKTREE" || gitdie "git worktree remove failed"
    else
      rig_git worktree remove "$WORKTREE" || gitdie "git worktree remove failed"
    fi
  fi
  if rig_git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    [[ "$(rig_git rev-parse "refs/heads/$BRANCH")" == "$(bead_meta "$ROOT" survey_cleanup_head)" ]] || die "branch changed since cleanup"
    rig_git branch -D "$BRANCH" >/dev/null || gitdie "could not delete survey branch"
  fi
  bead_set "$ROOT" "survey_cleanup=removed"
  info "removed $WORKTREE and local branch $BRANCH"
}

# --- main ------------------------------------------------------------------

sub="${1:-}"; shift || true
case "$sub" in
  prepare|show|stamp|publish|cleanup) resolve_identity ;;
esac
case "$sub" in
  prepare) cmd_prepare "$@" ;;
  show)    cmd_show "$@" ;;
  stamp)   cmd_stamp "$@" ;;
  publish) cmd_publish "$@" ;;
  cleanup) cmd_cleanup "$@" ;;
  -h|--help|"") usage; [[ -n "$sub" ]] && exit 0 || exit 2 ;;
  *) die "unknown subcommand '$sub'" ;;
esac
