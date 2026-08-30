#!/usr/bin/env bash
# Prototype of ship-docs: builds retain payloads from doc frontmatter per
# retain-contract.md and ships them to a bank. Usage: ./ship.sh <bank-id>
set -euo pipefail

BANK="${1:?usage: ship.sh <bank-id>}"
API="${HINDSIGHT_API:-https://hindsight-api.brandondennis.me}"
DIR="$(cd "$(dirname "$0")" && pwd)"
OPS_FILE="$DIR/operations.tsv"
: > "$OPS_FILE"

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

for doc in "$DIR"/docs/*.md; do
  name="$(basename "$doc")"
  fm() { yq --front-matter=extract "$1" "$doc"; }
  fmj() { yq --front-matter=extract -o=json "$1" "$doc"; }

  type="$(fm '.type')"
  doc_id="$(fm '.id')"
  title="$(fm '.title')"
  status="$(fm '.status // ""')"
  source="$(fm '.source // ""')"
  scope="$(fm '.scope')"
  updated="$(fm '.updated_at')"
  repos_json="$(fmj '.repos // []' | jq -c .)"
  domains_json="$(fmj '.domains // []' | jq -c .)"

  # --- shipper is the enforcement point ---
  strategy="$(strategy_for "$type")" || { echo "REFUSED $name: unknown type '$type'" >&2; exit 1; }
  case "$scope" in business|platform|repo) ;; *) echo "REFUSED $name: bad scope '$scope'" >&2; exit 1 ;; esac
  case "$source" in human|agent|external) ;; *) echo "REFUSED $name: bad source '$source'" >&2; exit 1 ;; esac
  if [[ -n "$status" ]]; then
    case "$status" in draft|accepted|superseded|deprecated) ;; *) echo "REFUSED $name: bad status '$status'" >&2; exit 1 ;; esac
  fi

  # --- content: body only, frontmatter stripped ---
  body="$(awk 'f{print} /^---[[:space:]]*$/{c++; if(c==2)f=1}' "$doc")"

  # --- context: one line, names the status when not accepted ---
  case "$type" in
    voice-memo) context="owner voice memo, unstructured thinking, not a decision" ;;
    *)
      context="$type: $title"
      [[ "$(jq -r 'join(", ")' <<<"$domains_json")" != "" ]] && context+=" — domain $(jq -r 'join(", ")' <<<"$domains_json")"
      [[ "$(jq -r 'join(", ")' <<<"$repos_json")" != "" ]] && context+=", repo $(jq -r 'join(", ")' <<<"$repos_json")"
      case "$status" in
        draft) context+="; DRAFT, a proposal subject to change, not current platform direction" ;;
        superseded) context+="; SUPERSEDED, retained as history, not current platform direction" ;;
        deprecated) context+="; DEPRECATED, no longer holds" ;;
      esac ;;
  esac

  # --- tags + observation_scopes ---
  tags="$(jq -cn \
    --arg scope "$scope" --arg type "$type" --arg source "$source" --arg status "$status" \
    --argjson repos "$repos_json" --argjson domains "$domains_json" '
    ["scope:\($scope)"]
    + ($repos | map("repo:\(.)"))
    + ($domains | map("domain:\(.)"))
    + ["memory_type:\($type)", "source:\($source)"]
    + (if $status != "" then ["status:\($status)"] else [] end)')"
  oscopes="$(jq -cn --arg scope "$scope" --argjson repos "$repos_json" --argjson domains "$domains_json" '
    ($domains | map(["domain:\(.)"])) + ($repos | map(["repo:\(.)"])) + [["scope:\($scope)"]]')"

  payload="$(jq -cn \
    --arg content "$body" --arg doc_id "$doc_id" --arg context "$context" \
    --arg ts "$updated" --arg strategy "$strategy" \
    --argjson tags "$tags" --argjson oscopes "$oscopes" '
    {items: [{content: $content, document_id: $doc_id, context: $context,
              timestamp: $ts, strategy: $strategy, tags: $tags,
              observation_scopes: $oscopes}],
     async: true}')"

  resp="$(curl -sS -X POST "$API/v1/default/banks/$BANK/memories" \
    -H 'Content-Type: application/json' -d "$payload")"
  op_id="$(jq -r '.operation_id // empty' <<<"$resp")"
  if [[ -z "$op_id" ]]; then
    echo "SHIP FAILED $name: $resp" >&2; exit 1
  fi
  printf '%s\t%s\t%s\n' "$doc_id" "$op_id" "$name" >> "$OPS_FILE"
  echo "shipped $doc_id  op=$op_id"
done

echo "all shipped; operations in $OPS_FILE"
