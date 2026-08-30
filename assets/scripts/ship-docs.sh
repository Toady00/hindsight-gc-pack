#!/usr/bin/env bash
# ship-docs.sh — stateless reconcile of published docs into a Hindsight bank.
#
# THE BANK IS THE LEDGER. There is no local state file. Every retain stamps
# document metadata the bank persists and returns from its paged document
# list:
#
#   document_metadata: { content_hash, repo, relpath }
#
# The shipper diffs published docs against that list, so any machine — a
# fresh clone included — computes the same answer, and two machines can
# never diverge: worst case both ship identical content, which the
# document_id replace absorbs idempotently.
#
# SHIP FROM THE REF, NOT THE TREE. For a docs root inside a git repo, the
# walker reads the committed content of the canonical ref via
# `git ls-tree -r` + `git show` — never the working tree. A branch switch,
# dirty checkout, or mid-rebase state at order time cannot leak unpublished
# content into the bank. Committing to the canonical ref IS the act of
# publishing (see the shipping contract in the pack README).
#
# Ref resolution per root: --ref override, else origin/HEAD (the remote's
# default branch, as already fetched — this script never fetches), else
# local HEAD (repos with no remote). A git repo with no commits publishes
# nothing. A root outside any git repo falls back to walking the
# filesystem — the pack works without git; git is the recommended publish
# boundary, not a requirement.
#
#   ship-docs.sh [options] <docs-root> [<docs-root>...]
#
# Options:
#   --bank <id>            bank to ship into (default: $HINDSIGHT_BANK;
#                          no hardcoded fallback — every city declares its
#                          own bank in [workspace] env)
#   --api <url>            Hindsight API base (default: $HINDSIGHT_API or prod)
#   --ref <ref>            ship this ref for every git root (default: auto)
#   --fetch                git fetch origin in each git root first, so
#                          origin/HEAD is current (fetch failure warns and
#                          ships as-fetched; never fatal). Off by default —
#                          the scheduled order opts in; ad-hoc runs decide.
#   --domains <file>       known-domains file, one per line (warn on new values)
#   --drain-timeout <sec>  max wait for in-flight bank operations (default 300)
#   --dry-run              validate + diff + report only, write nothing
#
# Exit codes:
#   0  reconciled (GONE reports are informational, never fatal)
#   1  docs refused (contract violations) or failed extraction
#   3  drain timeout — bank operations still in flight, nothing shipped
#
# Change detection: sha256 of the FULL published file (frontmatter
# included, so a status flip re-ships even without an updated_at bump) vs
# the bank's stamped document_metadata.content_hash. Absent or different
# => ship. The retained content is still the frontmatter-stripped body —
# the hash input is wider than the payload on purpose.
#
# Race rule (verified 2026-08-25): never re-retain a document_id while a
# prior operation is in flight — it orphans memories permanently. Guarded
# twice: a bank-level drain before shipping (cross-run) and a wait-to-
# terminal on every operation this run creates (intra-run).
#
# GONE detection: bank documents whose document_metadata.repo matches a
# walked root's repo but whose id was not published in the walk. Scoped to
# walked repos, so partial-root runs cannot false-positive. Report-only:
# the bank never auto-removes anything — deprecate-in-place is the only
# removal story.
#
# Scale note: the diff costs one paged list (limit 500). At ~10^5 docs
# revisit with tag-scoped listing; the LLM cost of one shipped doc dwarfs
# the listing either way.

set -euo pipefail

API="${HINDSIGHT_API:-https://hindsight-api.brandondennis.me}"
BANK=""
REF_OVERRIDE=""
DOMAINS_FILE=""
DRAIN_TIMEOUT=300
DRY_RUN=false
FETCH=false
ROOTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bank) BANK="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    --ref) REF_OVERRIDE="$2"; shift 2 ;;
    --domains) DOMAINS_FILE="$2"; shift 2 ;;
    --drain-timeout) DRAIN_TIMEOUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --fetch) FETCH=true; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) ROOTS+=("$1"); shift ;;
  esac
done
[[ -n "$BANK" ]] || BANK="${HINDSIGHT_BANK:-}"
[[ -n "$BANK" ]] || { echo "no bank: set HINDSIGHT_BANK in [workspace] env, or pass --bank <id>" >&2; exit 2; }
[[ ${#ROOTS[@]} -gt 0 ]] || { echo "at least one docs root is required" >&2; exit 2; }

TMP="$(mktemp -d)"
SEEN="$TMP/seen-ids.tsv"; : > "$SEEN"

# Known repos: the city rig list is the registry. Soft dependency — outside
# a city (or if gc is unavailable) we warn instead of validating.
KNOWN_REPOS=""
if command -v gc >/dev/null 2>&1; then
  KNOWN_REPOS="$(gc rig list --json 2>/dev/null | jq -r '.rigs[]?.name // empty' 2>/dev/null | sort -u || true)"
fi

strategy_for() {
  case "$1" in
    adr|spec|hld) echo design-record ;;
    prd|user-journey) echo product-doc ;;
    methodology) echo methodology ;;
    convention) echo convention ;;
    current-state) echo current-state ;;
    meeting-notes|discussion) echo discussion ;;
    voice-memo) echo voice-memo ;;
    build-report) echo build-report ;;
    runbook) echo operational-runbook ;;
    gotcha) echo gotcha ;;
    external) echo source-document ;;
    *) return 1 ;;
  esac
}

op_status() { hindsight -o json operation get "$BANK" "$1" 2>/dev/null | jq -r '.status // "unknown"'; }

wait_terminal() { # wait_terminal <op_id> -> prints final status
  local op="$1" st i
  for i in $(seq 1 90); do
    st="$(op_status "$op")"
    case "$st" in pending|processing) sleep 10 ;; *) echo "$st"; return 0 ;; esac
  done
  echo "timeout"
}

resolve_ref() { # $1 = repo toplevel -> echoes the ref to publish from
  if [[ -n "$REF_OVERRIDE" ]]; then echo "$REF_OVERRIDE"; return; fi
  local head
  head="$(git -C "$1" symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -n "$head" ]]; then echo "${head#refs/remotes/}"; return; fi
  echo "HEAD"
}

repo_ident() { # $1 = repo toplevel -> stable repo name for GONE attribution
  local url common base
  url="$(git -C "$1" remote get-url origin 2>/dev/null || true)"
  if [[ -n "$url" ]]; then base="$(basename "$url")"; echo "${base%.git}"; return; fi
  # Worktree layouts (.../project/.bare + sibling branch dirs) make the
  # toplevel basename the BRANCH name; the common git dir names the project.
  common="$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  if [[ -n "$common" ]]; then
    base="$(basename "$common")"
    case "$base" in
      .git|.bare) basename "$(dirname "$common")"; return ;;
      *.git) echo "${base%.git}"; return ;;
    esac
  fi
  basename "$1"
}

# ---------- 0. drain: never ship over in-flight operations ----------
inflight_count() {
  hindsight -o json operation list "$BANK" 2>/dev/null \
    | jq -r '[.. | objects | select(has("status")) | .status | select(. == "pending" or . == "processing")] | length' 2>/dev/null || echo 0
}
if $DRY_RUN; then
  n="$(inflight_count)"
  [[ "$n" -gt 0 ]] && echo "NOTE: $n operation(s) in flight (dry-run proceeds; a real run would drain first)"
else
  deadline=$((SECONDS + DRAIN_TIMEOUT))
  while :; do
    n="$(inflight_count)"
    [[ "$n" -eq 0 ]] && break
    if (( SECONDS >= deadline )); then
      echo "DRAIN TIMEOUT: $n operation(s) still in flight after ${DRAIN_TIMEOUT}s — shipping now could race them. Nothing shipped." >&2
      exit 3
    fi
    echo "draining: $n operation(s) in flight"; sleep 10
  done
fi

# ---------- 1. bank inventory: one paged list, the whole ledger ----------
BANK_ITEMS="$TMP/bank-items.json"
echo '[]' > "$BANK_ITEMS"
offset=0
while :; do
  curl -sS "$API/v1/default/banks/$BANK/documents?limit=500&offset=$offset" > "$TMP/page.json"
  count="$(jq '.items | length' "$TMP/page.json")"
  total="$(jq '.total' "$TMP/page.json")"
  jq -s '.[0] + .[1].items' "$BANK_ITEMS" "$TMP/page.json" > "$TMP/merged.json" && mv "$TMP/merged.json" "$BANK_ITEMS"
  offset=$((offset + count))
  [[ "$count" -eq 0 || "$offset" -ge "$total" ]] && break
done
jq 'map({key: .id, value: {hash: (.document_metadata.content_hash // ""), repo: (.document_metadata.repo // ""), relpath: (.document_metadata.relpath // "")}}) | from_entries' \
  "$BANK_ITEMS" > "$TMP/bank-map.json"
echo "bank inventory: $(jq 'length' "$TMP/bank-map.json") document(s)"

# ---------- 2. manifest: what is published, per root ----------
# Each line: repo_name <TAB> repo_base <TAB> ref <TAB> path
# ref == "-" means filesystem mode (root outside git); path is absolute.
MANIFEST="$TMP/manifest.tsv"; : > "$MANIFEST"
WALKED_REPOS_FILE="$TMP/walked-repos.txt"; : > "$WALKED_REPOS_FILE"

for root in "${ROOTS[@]}"; do
  [[ -d "$root" ]] || { echo "WARN: root not found: $root" >&2; continue; }
  root_abs="$(cd "$root" && pwd)"
  repo_base="$(git -C "$root_abs" rev-parse --show-toplevel 2>/dev/null || true)"

  if [[ -n "$repo_base" ]]; then
    if $FETCH && ! grep -qx "$repo_base" "$TMP/fetched.txt" 2>/dev/null; then
      echo "$repo_base" >> "$TMP/fetched.txt"
      if git -C "$repo_base" remote get-url origin >/dev/null 2>&1; then
        git -C "$repo_base" fetch origin --quiet 2>/dev/null \
          || echo "WARN: fetch failed for $repo_base — shipping as-fetched" >&2
      fi
    fi
    ref="$(resolve_ref "$repo_base")"
    repo_name="$(repo_ident "$repo_base")"
    if ! git -C "$repo_base" rev-parse --verify --quiet "$ref^{commit}" >/dev/null 2>&1; then
      echo "NOTE: $root_abs — ref '$ref' has no commits; nothing is published here yet"
      grep -qx "$repo_name" "$WALKED_REPOS_FILE" || echo "$repo_name" >> "$WALKED_REPOS_FILE"
      continue
    fi
    rootrel="${root_abs#"$repo_base"}"; rootrel="${rootrel#/}"
    if [[ -n "$rootrel" ]]; then
      listing="$(git -C "$repo_base" ls-tree -r --name-only "$ref" -- "$rootrel" | grep '\.md$' || true)"
    else
      listing="$(git -C "$repo_base" ls-tree -r --name-only "$ref" | grep '\.md$' || true)"
    fi
    echo "publishing from $repo_name@$ref ($root_abs)"
    while IFS= read -r p; do
      [[ -z "$p" ]] && continue
      printf '%s\t%s\t%s\t%s\n' "$repo_name" "$repo_base" "$ref" "$p" >> "$MANIFEST"
    done <<<"$listing"
  else
    repo_name="$(basename "$root_abs")"
    echo "publishing from filesystem ($root_abs — not a git repo)"
    while IFS= read -r -d '' f; do
      printf '%s\t%s\t-\t%s\n' "$repo_name" "$root_abs" "$f" >> "$MANIFEST"
    done < <(find "$root_abs" -name '*.md' -type f -print0)
  fi
  grep -qx "$repo_name" "$WALKED_REPOS_FILE" || echo "$repo_name" >> "$WALKED_REPOS_FILE"
done

# ---------- 3. validate, diff, ship ----------
SHIPPED=0; SKIPPED=0; REFUSED=0; FAILED=0
declare -a NEW_OPS=()      # "doc_id<TAB>op_id"

refuse() { echo "REFUSED  $1: $2" >&2; REFUSED=$((REFUSED+1)); }

while IFS=$'\t' read -r repo_name repo_base ref p; do
  if [[ "$ref" == "-" ]]; then
    FILE="$p"
    display="$p"
    relpath="${p#"$repo_base"/}"
  else
    FILE="$TMP/blob.md"
    git -C "$repo_base" show "$ref:$p" > "$FILE" 2>/dev/null || { echo "WARN: cannot read $ref:$p" >&2; continue; }
    display="$repo_name@$ref:$p"
    relpath="$p"
  fi

  head -1 "$FILE" | grep -q '^---[[:space:]]*$' || continue   # no frontmatter: not a shippable doc

  fm()  { yq --front-matter=extract "$1" "$FILE"; }
  fmj() { yq --front-matter=extract -o=json "$1" "$FILE"; }

  doc_id="$(fm '.id // ""')"
  type="$(fm '.type // ""')"
  [[ -n "$doc_id" && -n "$type" ]] || continue                # frontmatter but no id/type: not ours

  # duplicate id within this walk: two published files claiming one
  # document_id would silently last-writer-win in the bank. Refuse the second.
  dup="$(awk -F'\t' -v id="$doc_id" '$1 == id {print $2; exit}' "$SEEN")"
  if [[ -n "$dup" ]]; then
    refuse "$doc_id" "duplicate id: already seen at $dup, also claimed by $display"
    continue
  fi
  printf '%s\t%s\n' "$doc_id" "$display" >> "$SEEN"

  title="$(fm '.title // ""')"
  status="$(fm '.status // ""')"
  source="$(fm '.source // ""')"
  scope="$(fm '.scope // ""')"
  updated="$(fm '.updated_at // ""')"
  repos_json="$(fmj '.repos // []' | jq -c .)"
  domains_json="$(fmj '.domains // []' | jq -c .)"

  # ---- validation: the shipper is the enforcement point ----
  strategy="$(strategy_for "$type")" || { refuse "$doc_id" "unknown type '$type'"; continue; }
  case "$scope" in business|platform|repo) ;; *) refuse "$doc_id" "bad scope '$scope'"; continue ;; esac
  case "$source" in human|agent|external) ;; *) refuse "$doc_id" "bad source '$source'"; continue ;; esac
  if [[ -n "$status" ]]; then
    case "$status" in draft|accepted|superseded|deprecated) ;; *) refuse "$doc_id" "bad status '$status'"; continue ;; esac
  fi
  [[ -n "$updated" ]] || { refuse "$doc_id" "missing updated_at"; continue; }

  # repo: values against the rig registry (warn-only when registry absent)
  while IFS= read -r r; do
    [[ -z "$r" ]] && continue
    if [[ -n "$KNOWN_REPOS" ]] && ! grep -qx "$r" <<<"$KNOWN_REPOS"; then
      echo "WARN     $doc_id: repo '$r' matches no rig in this city" >&2
    fi
  done < <(jq -r '.[]' <<<"$repos_json")
  # domain: values against the vocab file (warn-only)
  if [[ -n "$DOMAINS_FILE" && -f "$DOMAINS_FILE" ]]; then
    while IFS= read -r d; do
      [[ -z "$d" ]] && continue
      grep -qx "$d" "$DOMAINS_FILE" || echo "WARN     $doc_id: new domain '$d' not in $DOMAINS_FILE" >&2
    done < <(jq -r '.[]' <<<"$domains_json")
  fi

  # ---- change detection: our hash vs the bank's stamped hash ----
  hash="$(shasum -a 256 "$FILE" | cut -d' ' -f1)"
  bank_hash="$(jq -r --arg id "$doc_id" '.[$id].hash // "absent"' "$TMP/bank-map.json")"
  if [[ "$hash" == "$bank_hash" ]]; then SKIPPED=$((SKIPPED+1)); continue; fi

  if $DRY_RUN; then echo "WOULD SHIP $doc_id ($display)"; SHIPPED=$((SHIPPED+1)); continue; fi

  # ---- payload per retain-contract.md ----
  body="$(awk 'f{print} /^---[[:space:]]*$/{c++; if(c==2)f=1}' "$FILE")"
  if [[ "$type" == "voice-memo" ]]; then
    context="owner voice memo, unstructured thinking, not a decision"
  else
    context="$type: $title"
    dj="$(jq -r 'join(", ")' <<<"$domains_json")"; [[ -n "$dj" ]] && context+=" — domain $dj"
    rj="$(jq -r 'join(", ")' <<<"$repos_json")";   [[ -n "$rj" ]] && context+=", repo $rj"
    case "$status" in
      draft) context+="; DRAFT, a proposal subject to change, not current platform direction" ;;
      superseded) context+="; SUPERSEDED, retained as history, not current platform direction" ;;
      deprecated) context+="; DEPRECATED, no longer holds" ;;
    esac
  fi
  tags="$(jq -cn --arg scope "$scope" --arg type "$type" --arg source "$source" --arg status "$status" \
    --argjson repos "$repos_json" --argjson domains "$domains_json" '
    ["scope:\($scope)"] + ($repos | map("repo:\(.)")) + ($domains | map("domain:\(.)"))
    + ["memory_type:\($type)", "source:\($source)"]
    + (if $status != "" then ["status:\($status)"] else [] end)')"
  oscopes="$(jq -cn --arg scope "$scope" --argjson repos "$repos_json" --argjson domains "$domains_json" '
    ($domains | map(["domain:\(.)"])) + ($repos | map(["repo:\(.)"])) + [["scope:\($scope)"]]')"
  payload="$(jq -cn --arg content "$body" --arg doc_id "$doc_id" --arg context "$context" \
    --arg ts "$updated" --arg strategy "$strategy" \
    --arg hash "$hash" --arg repo "$repo_name" --arg relpath "$relpath" \
    --argjson tags "$tags" --argjson oscopes "$oscopes" '
    {items: [{content: $content, document_id: $doc_id, context: $context, timestamp: $ts,
              strategy: $strategy, tags: $tags, observation_scopes: $oscopes,
              metadata: {content_hash: $hash, repo: $repo, relpath: $relpath}}],
     async: true}')"

  resp="$(curl -sS -X POST "$API/v1/default/banks/$BANK/memories" \
    -H 'Content-Type: application/json' -d "$payload")"
  op_id="$(jq -r '.operation_id // empty' <<<"$resp")"
  if [[ -z "$op_id" ]]; then
    echo "FAILED   $doc_id: ship rejected: $resp" >&2; FAILED=$((FAILED+1)); continue
  fi
  NEW_OPS+=("$doc_id	$op_id")
  echo "SHIPPED  $doc_id  op=$op_id"
  SHIPPED=$((SHIPPED+1))
done < "$MANIFEST"

# ---------- 4. wait this run's operations to terminal ----------
if ! $DRY_RUN && [[ ${#NEW_OPS[@]} -gt 0 ]]; then
  for row in "${NEW_OPS[@]}"; do
    doc_id="${row%%	*}"; op_id="${row#*	}"
    st="$(wait_terminal "$op_id")"
    if [[ "$st" != "completed" ]]; then
      err="$(hindsight -o json operation get "$BANK" "$op_id" 2>/dev/null | jq -r '.error_message // "n/a"')"
      echo "FAILED   $doc_id: op $op_id ended '$st': $err" >&2
      FAILED=$((FAILED+1)); SHIPPED=$((SHIPPED-1))
      # No rollback needed: the bank's stamp only updates on completion, so
      # the next run re-detects the diff and re-ships. Statelessness IS the
      # retry mechanism.
    fi
  done
fi

# ---------- 5. gone report: in the bank, not published in the walk ----------
GONE=0
while IFS=$'\t' read -r id grepo grel; do
  awk -F'\t' -v id="$id" '$1 == id {found=1} END {exit !found}' "$SEEN" && continue
  echo "GONE     $id: bank holds it (repo=$grepo, relpath=$grel) but it is not published in the walk. Restore it or mark it status: deprecated — nothing is auto-removed." >&2
  GONE=$((GONE+1))
done < <(jq -r --slurpfile walked <(jq -R . "$WALKED_REPOS_FILE" | jq -s .) '
  .[] | select((.document_metadata.repo // "") as $r | $walked[0] | index($r)) |
  "\(.id)\t\(.document_metadata.repo)\t\(.document_metadata.relpath // "?")"' "$BANK_ITEMS")

echo "---"
echo "shipped=$SHIPPED skipped=$SKIPPED refused=$REFUSED failed=$FAILED gone=$GONE"
[[ $REFUSED -eq 0 && $FAILED -eq 0 ]] || exit 1
