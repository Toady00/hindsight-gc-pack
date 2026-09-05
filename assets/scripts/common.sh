#!/usr/bin/env bash
# Shared connection, writer admission, and operation handling. Source only.
HINDSIGHT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

hs_connect() {
  local connection
  connection="$(python3 "$HINDSIGHT_SCRIPT_DIR/connection.py" "${API:-}")" || return 2
  API="$(jq -r .api <<<"$connection")"
  export HINDSIGHT_API="$API" HINDSIGHT_API_URL="$API"
  export HINDSIGHT_API_KEY="$(jq -r .key <<<"$connection")"
  BANK_PATH="$(jq -rn --arg bank "$BANK" '$bank | @uri')"
}

hs_http() {
  # Pass the credential over stdin rather than exposing it in curl's argv.
  local header
  header="$(jq -rn 'env.HINDSIGHT_API_KEY // "" | if . == "" then "" else "header = " + ("Authorization: Bearer " + . | tojson) end')"
  curl --config - --silent --show-error --fail-with-body --connect-timeout 15 --max-time 120 \
    "$@" <<<"$header"
}

hs_is_writer() {
  [[ "${HINDSIGHT_WRITER:-}" == "archivist" && -n "${GC_SESSION_ID:-}" ]]
}

hs_require_writer() {
  hs_is_writer && return 0
  echo "writes require the managed archivist session; use gc hindsight ship to queue shipping, or send a memory proposal to the archivist" >&2
  return 2
}

hs_inflight_count() {
  # The CLI lists only a recent page. Ask for each active status explicitly;
  # total covers the complete filtered set, even if an old operation is stuck.
  local status response count total=0
  for status in pending processing; do
    response="$(hs_http "$API/v1/default/banks/$BANK_PATH/operations?status=$status&limit=1")" || return 1
    count="$(jq -er --arg status "$status" '
      if type == "object" and (.total | type == "number" and . >= 0 and floor == .)
        and (.operations | type == "array" and all(.[]; .status == $status))
      then .total else error("invalid filtered operation response") end' <<<"$response")" || return 1
    total=$((total + count))
  done
  echo "$total"
}

hs_drain() {
  local deadline=$((SECONDS + DRAIN_TIMEOUT)) n
  while :; do
    n="$(hs_inflight_count)" || { echo "DRAIN FAILED: cannot establish whether operations are in flight; nothing new will be written" >&2; return 5; }
    [[ "$n" -eq 0 ]] && return 0
    if (( SECONDS >= deadline )); then
      echo "DRAIN TIMEOUT: $n operation(s) still in flight" >&2
      return 3
    fi
    echo "draining: $n operation(s) in flight" >&2
    sleep 5
  done
}

hs_wait_terminal() {
  local op="$1" response st i
  for i in $(seq 1 90); do
    response="$(hindsight -o json operation get "$BANK" "$op")" || return 5
    st="$(jq -er '.status | select(type == "string")' <<<"$response")" || return 5
    case "$st" in
      pending|processing) sleep 10 ;;
      completed) echo completed; return 0 ;;
      failed|cancelled|canceled) jq -r '.error_message // "operation failed"' <<<"$response" >&2; echo "$st"; return 0 ;;
      *) echo "unknown operation status: $st" >&2; return 5 ;;
    esac
  done
  echo timeout
}
