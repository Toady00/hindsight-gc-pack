#!/usr/bin/env bash
# ship-docs.sh — reconcile docs trees into a Hindsight memory bank.
#
# The single ship path for the bank (retain-contract.md, "Shipping
# cadence"). Walks docs roots, validates frontmatter, ships every doc whose
# content changed since its last shipped state, serializes per document_id,
# and polls operations to terminal status.
#
#   ship-docs.sh --bank <bank> [options] <docs-root> [<docs-root>...]
#
# Options:
#   --bank <id>       bank to ship into (required)
#   --api <url>       Hindsight API base (default: $HINDSIGHT_API or prod)
#   --state <file>    state file (default: .gc/hindsight/ship-state.<bank>.json)
#   --domains <file>  known-domains file, one per line (warn on new values)
#   --dry-run         validate + report only, write nothing
#
# Design notes, learned the hard way (see retain-contract.md):
#   * Enforcement point: refuses unknown type/status/source/scope loudly.
#   * Per-doc serialization: never re-retains a document_id whose previous
#     operation is still pending/processing (that orphans memories
#     permanently — verified 2026-08-25 on a test bank).
#   * Over-shipping is harmless (idempotent document_id replace); missed
#     shipping is the silent failure. When in doubt, this script ships.
#   * repo: tag values are validated against `gc rig list` when available —
#     the city's rig list is the tag registry (rig name == repo tag).
#
# Prototype lineage: test/ship.sh in this pack was validated end-to-end
# against a live test bank; this script extends it with state tracking,
# serialization, vocab checks, and multi-root walking.

set -euo pipefail

API="${HINDSIGHT_API:-https://hindsight-api.brandondennis.me}"
BANK=""
STATE=""
DOMAINS_FILE=""
DRY_RUN=false
ROOTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bank) BANK="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    --state) STATE="$2"; shift 2 ;;
    --domains) DOMAINS_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) ROOTS+=("$1"); shift ;;
  esac
done
[[ -n "$BANK" ]] || { echo "--bank is required" >&2; exit 2; }
[[ ${#ROOTS[@]} -gt 0 ]] || { echo "at least one docs root is required" >&2; exit 2; }
STATE="${STATE:-.gc/hindsight/ship-state.$BANK.json}"
mkdir -p "$(dirname "$STATE")"
[[ -f "$STATE" ]] || echo '{}' > "$STATE"

# Known repos: the city rig list is the registry. Soft dependency — outside
# a city (or if gc is unavailable) we warn instead of validating.
KNOWN_REPOS=""
if command -v gc >/dev/null 2>&1; then
  KNOWN_REPOS="$(gc rig list --json 2>/dev/null | jq -r '.[].name' 2>/dev/null | sort -u || true)"
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
  for i in $(seq 1 60); do
    st="$(op_status "$op")"
    case "$st" in pending|processing) sleep 5 ;; *) echo "$st"; return 0 ;; esac
  done
  echo "timeout"
}

SHIPPED=0; SKIPPED=0; REFUSED=0; FAILED=0
declare -a NEW_OPS=()   # "doc_id<TAB>op_id"

refuse() { echo "REFUSED  $1: $2" >&2; REFUSED=$((REFUSED+1)); }

for root in "${ROOTS[@]}"; do
  [[ -d "$root" ]] || { echo "WARN: root not found: $root" >&2; continue; }
  while IFS= read -r -d '' doc; do
    head -1 "$doc" | grep -q '^---[[:space:]]*$' || continue   # no frontmatter: not a shippable doc

    fm()  { yq --front-matter=extract "$1" "$doc"; }
    fmj() { yq --front-matter=extract -o=json "$1" "$doc"; }

    doc_id="$(fm '.id // ""')"
    type="$(fm '.type // ""')"
    [[ -n "$doc_id" && -n "$type" ]] || continue               # frontmatter but no id/type: not ours

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

    # ---- change detection ----
    hash="$(shasum -a 256 "$doc" | cut -d' ' -f1)"
    prev_hash="$(jq -r --arg id "$doc_id" '.[$id].hash // ""' "$STATE")"
    if [[ "$hash" == "$prev_hash" ]]; then SKIPPED=$((SKIPPED+1)); continue; fi

    if $DRY_RUN; then echo "WOULD SHIP $doc_id ($doc)"; SHIPPED=$((SHIPPED+1)); continue; fi

    # ---- per-doc serialization: prior op must be terminal ----
    prev_op="$(jq -r --arg id "$doc_id" '.[$id].op // ""' "$STATE")"
    if [[ -n "$prev_op" ]]; then
      st="$(wait_terminal "$prev_op")"
      [[ "$st" == "timeout" ]] && { echo "FAILED   $doc_id: previous op $prev_op not terminal, refusing to race it" >&2; FAILED=$((FAILED+1)); continue; }
    fi

    # ---- payload per retain-contract.md ----
    body="$(awk 'f{print} /^---[[:space:]]*$/{c++; if(c==2)f=1}' "$doc")"
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
      --arg ts "$updated" --arg strategy "$strategy" --argjson tags "$tags" --argjson oscopes "$oscopes" '
      {items: [{content: $content, document_id: $doc_id, context: $context, timestamp: $ts,
                strategy: $strategy, tags: $tags, observation_scopes: $oscopes}], async: true}')"

    resp="$(curl -sS -X POST "$API/v1/default/banks/$BANK/memories" \
      -H 'Content-Type: application/json' -d "$payload")"
    op_id="$(jq -r '.operation_id // empty' <<<"$resp")"
    if [[ -z "$op_id" ]]; then
      echo "FAILED   $doc_id: ship rejected: $resp" >&2; FAILED=$((FAILED+1)); continue
    fi

    # Record op immediately; hash only after the op completes (below), so a
    # failed extraction re-ships next run instead of being silently lost.
    tmp="$(mktemp)"; jq --arg id "$doc_id" --arg op "$op_id" \
      '.[$id] = ((.[$id] // {}) + {op: $op, op_status: "pending"})' "$STATE" > "$tmp" && mv "$tmp" "$STATE"
    NEW_OPS+=("$doc_id	$op_id	$hash")
    echo "SHIPPED  $doc_id  op=$op_id"
    SHIPPED=$((SHIPPED+1))
  done < <(find "$root" -name '*.md' -type f -print0)
done

# ---- drain this run's operations; commit hashes only on completion ----
if ! $DRY_RUN && [[ ${#NEW_OPS[@]} -gt 0 ]]; then
  for row in "${NEW_OPS[@]}"; do
    doc_id="${row%%	*}"; rest="${row#*	}"; op_id="${rest%%	*}"; hash="${rest#*	}"
    st="$(wait_terminal "$op_id")"
    tmp="$(mktemp)"
    if [[ "$st" == "completed" ]]; then
      jq --arg id "$doc_id" --arg op "$op_id" --arg h "$hash" --arg st "$st" \
        '.[$id] = {hash: $h, op: $op, op_status: $st, shipped_at: (now | todate)}' "$STATE" > "$tmp" && mv "$tmp" "$STATE"
    else
      jq --arg id "$doc_id" --arg op "$op_id" --arg st "$st" \
        '.[$id] = ((.[$id] // {}) + {op: $op, op_status: $st})' "$STATE" > "$tmp" && mv "$tmp" "$STATE"
      err="$(hindsight -o json operation get "$BANK" "$op_id" 2>/dev/null | jq -r '.error_message // "n/a"')"
      echo "FAILED   $doc_id: op $op_id ended '$st': $err" >&2
      FAILED=$((FAILED+1)); SHIPPED=$((SHIPPED-1))
    fi
  done
fi

echo "---"
echo "shipped=$SHIPPED skipped=$SKIPPED refused=$REFUSED failed=$FAILED"
[[ $REFUSED -eq 0 && $FAILED -eq 0 ]] || exit 1
