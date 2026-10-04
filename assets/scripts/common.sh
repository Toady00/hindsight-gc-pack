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

hs_http_read() {
  # Bounded retries for idempotent reads. Retry transport failures and 5xx,
  # but fail promptly for authentication, validation, and other 4xx responses.
  local attempt rc status delay=1 max_time remaining sleep_for initial=true
  for attempt in 1 2 3 4; do
    max_time=20
    if [[ -n "${HS_HTTP_DEADLINE:-}" ]]; then
      remaining=$((HS_HTTP_DEADLINE - SECONDS))
      if (( remaining <= 0 )); then
        if [[ "$initial" != true || "${HS_HTTP_ALLOW_INITIAL:-false}" != true ]]; then
          rc=28
          break
        fi
      else
        # SECONDS has whole-second resolution; leave a tick of headroom where
        # possible so the request does not routinely run past its phase limit.
        (( remaining > 1 )) && remaining=$((remaining - 1))
        (( max_time > remaining )) && max_time=$remaining
      fi
    fi
    : > "$HS_HTTP_BODY"
    : > "$HS_HTTP_ERROR"
    status="$(curl --config <(jq -rn 'env.HINDSIGHT_API_KEY // "" | if . == "" then "" else "header = " + ("Authorization: Bearer " + . | tojson) end') \
      --silent --show-error --fail-with-body --connect-timeout 5 --max-time "$max_time" \
      --output "$HS_HTTP_BODY" --write-out '%{http_code}' "$@" </dev/null 2>"$HS_HTTP_ERROR")" && rc=0 || rc=$?
    initial=false
    if [[ "$rc" -eq 0 ]]; then cat "$HS_HTTP_BODY"; return 0; fi
    if [[ "$status" =~ ^[0-9]{3}$ && "$status" -ge 400 && "$status" -lt 500 \
      && "$status" != 408 && "$status" != 425 && "$status" != 429 ]]; then
      cat "$HS_HTTP_BODY" >&2; cat "$HS_HTTP_ERROR" >&2; return "$rc"
    fi
    if (( attempt < 4 )); then
      if [[ -n "${HS_HTTP_DEADLINE:-}" ]]; then
        remaining=$((HS_HTTP_DEADLINE - SECONDS))
        if (( remaining <= 0 )); then
          rc=28
          break
        fi
        sleep_for=$delay
        (( sleep_for > remaining )) && sleep_for=$remaining
      else
        sleep_for=$delay
      fi
      sleep "$sleep_for"
      delay=$((delay * 2))
    fi
  done
  cat "$HS_HTTP_BODY" >&2; cat "$HS_HTTP_ERROR" >&2
  return "$rc"
}

hs_http_post_consolidate() {
  # Retry only failures that prove curl never connected to the API (DNS or
  # connection establishment). A timeout/reset after connect is ambiguous.
  local attempt rc delay=1
  for attempt in 1 2 3; do
    : > "$HS_HTTP_BODY"
    : > "$HS_HTTP_ERROR"
    if curl --config <(jq -rn 'env.HINDSIGHT_API_KEY // "" | if . == "" then "" else "header = " + ("Authorization: Bearer " + . | tojson) end') \
      --silent --show-error --fail-with-body --connect-timeout 5 --max-time 20 \
      --request POST --output "$HS_HTTP_BODY" "$1" </dev/null 2>"$HS_HTTP_ERROR"; then
      cat "$HS_HTTP_BODY"; return 0
    else
      rc=$?
    fi
    if [[ "$rc" != 5 && "$rc" != 6 && "$rc" != 7 ]]; then
      cat "$HS_HTTP_BODY" >&2; cat "$HS_HTTP_ERROR" >&2; return "$rc"
    fi
    if (( attempt < 3 )); then sleep "$delay"; delay=$((delay * 2)); fi
  done
  cat "$HS_HTTP_ERROR" >&2
  return "$rc"
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
    HS_HTTP_BODY="$(mktemp)" HS_HTTP_ERROR="$(mktemp)"
    local rc
    if response="$(hs_http_read "$API/v1/default/banks/$BANK_PATH/operations?status=$status&limit=1")"; then rc=0; else rc=$?; fi
    rm -f "$HS_HTTP_BODY" "$HS_HTTP_ERROR"
    (( rc == 0 )) || return 1
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
  local op="$1" timeout="${2:-900}" response st remaining sleep_for initial_poll=true known_inflight=false
  local deadline=$((SECONDS + timeout))
  while :; do
    HS_HTTP_BODY="$(mktemp)" HS_HTTP_ERROR="$(mktemp)"
    HS_HTTP_DEADLINE="$deadline"
    $initial_poll && HS_HTTP_ALLOW_INITIAL=true || unset HS_HTTP_ALLOW_INITIAL
    local rc
    if response="$(hs_http_read "$API/v1/default/banks/$BANK_PATH/operations/$op")"; then rc=0; else rc=$?; fi
    initial_poll=false
    unset HS_HTTP_DEADLINE HS_HTTP_ALLOW_INITIAL
    rm -f "$HS_HTTP_BODY" "$HS_HTTP_ERROR"
    if (( rc != 0 )); then
      if $known_inflight && (( rc == 28 && SECONDS >= deadline )); then echo timeout; return 0; fi
      echo "operation status read failed for $op" >&2
      return 5
    fi
    st="$(jq -er '.status | select(type == "string")' <<<"$response")" || {
      echo "invalid operation status response for $op" >&2
      return 5
    }
    case "$st" in
      pending|processing)
        known_inflight=true
        remaining=$((deadline - SECONDS))
        if (( remaining <= 0 )); then echo timeout; return 0; fi
        if (( remaining > 1 )); then
          (( remaining > 11 )) && remaining=11
          sleep_for=$((remaining - 1))
        else
          sleep_for=0.2
        fi
        sleep "$sleep_for" ;;
      completed) echo completed; return 0 ;;
      failed|cancelled|canceled) jq -r '.error_message // "operation failed"' <<<"$response" >&2; echo "$st"; return 0 ;;
      *) echo "unknown operation status: $st" >&2; return 5 ;;
    esac
  done
}
