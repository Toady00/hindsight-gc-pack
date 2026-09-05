#!/usr/bin/env bash
# memory-retain.sh — the write path for arbitrated agent memories.
#
# BANK-NATIVE, NO FILES. The docs corpus ships from git via ship-docs.sh;
# agent memories (gotchas and future agent-memory types) live only in the
# bank, like voice memos, with durable ingestion receipts in city Beads.
# This script is the ONLY way they get written:
# it builds the contract-compliant payload, refuses to race a pending
# operation for the same document_id (re-retaining while one is pending
# orphans memories PERMANENTLY — verified 2026-08-25), and polls its own
# operation to terminal.
#
#   memory-retain.sh --id <doc-id> --title <t> [options] < content.md
#   memory-retain.sh --id <doc-id> --bump [--content-file <f>]
#
# Options:
#   --id <id>              document id, e.g. gotcha.tflint-cache-lock (required)
#   --type <t>             doc type (default: gotcha; must be a contract type)
#   --title <t>            title (required for new retains)
#   --repos <a,b>          rig names this memory bites (repo: tags — retrieval)
#   --domains <a,b>        bounded contexts, if any
#   --scope <s>            business|platform|repo (default: repo)
#   --status <s>           draft|accepted|superseded|deprecated (new arbitrated memories default accepted)
#   --source <s>           human|agent|external (default: agent)
#   --bump                 repeat report: fetch the existing doc, increment
#                          hit_count, refresh the report line and timestamp,
#                          re-retain the same id. Content is reused unless
#                          --content-file provides merged content
#   --content-file <f>     content from a file instead of stdin
#   --bank <id>            bank (default: $HINDSIGHT_BANK; no fallback)
#   --api <url>            Hindsight API base (default: $HINDSIGHT_API, else ~/.hindsight/config api_url; never a hardcoded server)
#   --drain-timeout <sec>  max wait for in-flight operations (default 300)
#   --dry-run              print the payload, write nothing
#
# Metadata contract: {kind: "agent-memory", hit_count: N}. Deliberately NO
# `repo` metadata key — that key marks tree-walked docs and drives the
# shipper's GONE report; bank-native memories must never trip it. The
# repo: retrieval TAGS are unaffected and required for rig discovery.
set -euo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$PACK_DIR/assets/scripts/common.sh"
API=""
BANK="" ID="" TYPE="" TITLE="" REPOS="" DOMAINS="" SCOPE="" SOURCE="" STATUS=""
BUMP=false CONTENT_FILE="" DRY_RUN=false DRAIN_TIMEOUT=300

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id) ID="$2"; shift 2 ;;
    --type) TYPE="$2"; shift 2 ;;
    --title) TITLE="$2"; shift 2 ;;
    --repos) REPOS="$2"; shift 2 ;;
    --domains) DOMAINS="$2"; shift 2 ;;
    --scope) SCOPE="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --status) STATUS="$2"; shift 2 ;;
    --bump) BUMP=true; shift ;;
    --content-file) CONTENT_FILE="$2"; shift 2 ;;
    --bank) BANK="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    --drain-timeout) DRAIN_TIMEOUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$BANK" ]] || BANK="${HINDSIGHT_BANK:-}"
[[ -n "$BANK" ]] || { echo "no bank: set HINDSIGHT_BANK in [workspace] env, or pass --bank <id>" >&2; exit 2; }
hs_connect
$DRY_RUN || hs_require_writer
[[ -n "$ID" ]] || { echo "--id is required" >&2; exit 2; }
[[ "$DRAIN_TIMEOUT" =~ ^[0-9]+$ ]] || { echo "invalid drain timeout" >&2; exit 2; }
# Drain before reading the prior document for a bump, not just before POST.
$DRY_RUN || hs_drain || exit $?
if ! $DRY_RUN; then
  recovery="$(python3 "$PACK_DIR/assets/scripts/ingestion_cli.py" recover "$BANK" "$ID")" || exit $?
  case "$recovery" in
    recovered)
      echo "RECOVERED $ID: prior attempt completed; no additional hit counted; run again for a separate report"
      exit 0 ;;
    idle) ;;
    *) echo "invalid ingestion recovery response; nothing new will be written" >&2; exit 5 ;;
  esac
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# ---------- resolve content, count, and (on bump) prior fields ----------
HIT_COUNT=1
if $BUMP; then
  # The bank is the source of truth for bank-native memories: fetch the
  # existing doc for its content, count, and tags-derived fields.
  hindsight -o json document get "$BANK" "$ID" > "$TMP/doc.json" 2>/dev/null \
    || { echo "bump: cannot fetch document '$ID' from bank '$BANK'" >&2; exit 3; }
  prev_count="$(jq -r '.document_metadata.hit_count // .metadata.hit_count // 1' "$TMP/doc.json")"
  [[ "$prev_count" =~ ^[0-9]+$ ]] || { echo "invalid stored hit_count" >&2; exit 3; }
  HIT_COUNT=$((prev_count + 1))
  if [[ -n "$CONTENT_FILE" ]]; then
    CONTENT="$(cat "$CONTENT_FILE")"
  else
    CONTENT="$(jq -r '.content // .original_text // empty' "$TMP/doc.json")"
    [[ -n "$CONTENT" ]] || { echo "bump: document '$ID' has no readable content; pass --content-file" >&2; exit 3; }
  fi
  # Reconstruct title/type/scope/source/repos/domains from the stored doc
  # unless overridden on the command line.
  [[ -n "$TITLE" ]] || TITLE="$(jq -r '.document_metadata.title // .retain_params.metadata.title // ""' "$TMP/doc.json")"
  [[ -n "$TITLE" ]] || { echo "bump: stored doc has no metadata.title; pass --title" >&2; exit 3; }
  tags_json="$(jq -c '.tags // []' "$TMP/doc.json")"
  [[ -n "$REPOS" ]]   || REPOS="$(jq -r '[.[] | select(startswith("repo:")) | sub("^repo:";"")] | join(",")' <<<"$tags_json")"
  [[ -n "$DOMAINS" ]] || DOMAINS="$(jq -r '[.[] | select(startswith("domain:")) | sub("^domain:";"")] | join(",")' <<<"$tags_json")"
  [[ -n "$SCOPE" ]] || SCOPE="$(jq -r '[.[] | select(startswith("scope:")) | sub("^scope:";"")] | first // "'"$SCOPE"'"' <<<"$tags_json")"
  [[ -n "$SOURCE" ]] || SOURCE="$(jq -r '[.[] | select(startswith("source:")) | sub("^source:";"")] | first // "'"$SOURCE"'"' <<<"$tags_json")"
  [[ -n "$TYPE" ]] || TYPE="$(jq -r '[.[] | select(startswith("memory_type:")) | sub("^memory_type:";"")] | first // "'"$TYPE"'"' <<<"$tags_json")"
  [[ -n "$STATUS" ]] || STATUS="$(jq -r '[.[] | select(startswith("status:")) | sub("^status:";"")] | first // ""' <<<"$tags_json")"
  [[ -n "$STATUS" ]] || { echo "legacy memory has no status; choose --status explicitly" >&2; exit 2; }
else
  TYPE="${TYPE:-gotcha}"; SCOPE="${SCOPE:-repo}"; SOURCE="${SOURCE:-agent}"; STATUS="${STATUS:-accepted}"
  [[ -n "$TITLE" ]] || { echo "--title is required for new retains" >&2; exit 2; }
  if [[ -n "$CONTENT_FILE" ]]; then CONTENT="$(cat "$CONTENT_FILE")"; else CONTENT="$(cat)"; fi
  [[ -n "$CONTENT" ]] || { echo "no content (stdin or --content-file)" >&2; exit 2; }
fi

# ---------- report line: owned by this script, reader-visible ----------
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
times_word="times"; [[ "$HIT_COUNT" -eq 1 ]] && times_word="time"
CONTENT="$(sed '/^Reported [0-9][0-9]* times\{0,1\} (last: .*).$/d' <<<"$CONTENT")"
CONTENT="$(printf '%s\n\nReported %s %s (last: %s).\n' "$(printf '%s' "$CONTENT" | sed -e 's/[[:space:]]*$//')" "$HIT_COUNT" "$times_word" "$NOW")"

# ---------- contract payload ----------
repos_json="$(jq -cRn --arg s "$REPOS" '$s | split(",") | map(select(length>0))')"
domains_json="$(jq -cRn --arg s "$DOMAINS" '$s | split(",") | map(select(length>0))')"
verdict="$(jq -cn --arg id "$ID" --arg title "$TITLE" --arg type "$TYPE" --arg scope "$SCOPE" --arg source "$SOURCE" --arg status "$STATUS" --arg updated_at "$NOW" --argjson repos "$repos_json" --argjson domains "$domains_json" \
  '{id:$id,title:$title,type:$type,scope:$scope,source:$source,status:$status,updated_at:$updated_at,repos:$repos,domains:$domains}' \
  | python3 "$PACK_DIR/schemas/docs/validate.py")"
[[ "$(jq -r .verdict <<<"$verdict")" == "ship" ]] || { jq -r '.reason // "invalid memory"' <<<"$verdict" >&2; exit 2; }
STRATEGY="$(jq -r .strategy <<<"$verdict")"
context="$(jq -r .context <<<"$verdict"); agent-learned, arbitrated by the archivist"
tags="$(jq -c .tags <<<"$verdict")"
oscopes="$(jq -c .observation_scopes <<<"$verdict")"
# metadata values must be strings (API rejects numbers) — hit_count rides
# as a string and is parsed back to int on --bump. title is stored so a
# bump can rebuild the context line without parsing it back apart.
payload="$(jq -cn --arg content "$CONTENT" --arg doc_id "$ID" --arg context "$context" \
  --arg ts "$NOW" --arg strategy "$STRATEGY" --arg hits "$HIT_COUNT" --arg title "$TITLE" \
  --argjson tags "$tags" --argjson oscopes "$oscopes" '
  {items: [{content: $content, document_id: $doc_id, context: $context, timestamp: $ts,
            strategy: $strategy, tags: $tags, observation_scopes: $oscopes,
            metadata: {kind: "agent-memory", hit_count: $hits, title: $title}}],
   async: true}')"

if $DRY_RUN; then
  echo "WOULD RETAIN $ID (hit_count=$HIT_COUNT)"
  jq . <<<"$payload"
  exit 0
fi

result="$(python3 "$PACK_DIR/assets/scripts/ingestion_cli.py" retain "$BANK" <<<"$payload")" || exit $?
echo "$result hit_count=$HIT_COUNT"
