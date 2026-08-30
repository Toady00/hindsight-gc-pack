#!/usr/bin/env bash
# memory-retain.sh — the write path for arbitrated agent memories.
#
# BANK-NATIVE, NO FILES. The docs corpus ships from git via ship-docs.sh;
# agent memories (gotchas and future agent-memory types) live only in the
# bank, like voice memos. This script is the ONLY way they get written:
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
#   --source <s>           human|agent|external (default: agent)
#   --bump                 repeat report: fetch the existing doc, increment
#                          hit_count, refresh the report line and timestamp,
#                          re-retain the same id. Content is reused unless
#                          --content-file provides merged content
#   --content-file <f>     content from a file instead of stdin
#   --bank <id>            bank (default: $HINDSIGHT_BANK; no fallback)
#   --api <url>            Hindsight API base (default: $HINDSIGHT_API or prod)
#   --drain-timeout <sec>  max wait for in-flight operations (default 300)
#   --dry-run              print the payload, write nothing
#
# Metadata contract: {kind: "agent-memory", hit_count: N}. Deliberately NO
# `repo` metadata key — that key marks tree-walked docs and drives the
# shipper's GONE report; bank-native memories must never trip it. The
# repo: retrieval TAGS are unaffected and required for rig discovery.
set -euo pipefail

API="${HINDSIGHT_API:-https://hindsight-api.brandondennis.me}"
BANK="" ID="" TYPE="gotcha" TITLE="" REPOS="" DOMAINS="" SCOPE="repo" SOURCE="agent"
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
[[ -n "$ID" ]] || { echo "--id is required" >&2; exit 2; }

# Same type table as ship-docs.sh — one contract, two write paths.
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

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# ---------- resolve content, count, and (on bump) prior fields ----------
HIT_COUNT=1
if $BUMP; then
  # The bank is the source of truth for bank-native memories: fetch the
  # existing doc for its content, count, and tags-derived fields.
  hindsight -o json document get "$BANK" "$ID" > "$TMP/doc.json" 2>/dev/null \
    || { echo "bump: cannot fetch document '$ID' from bank '$BANK'" >&2; exit 3; }
  prev_count="$(jq -r '.document_metadata.hit_count // .metadata.hit_count // 1' "$TMP/doc.json")"
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
  SCOPE="$(jq -r '[.[] | select(startswith("scope:")) | sub("^scope:";"")] | first // "'"$SCOPE"'"' <<<"$tags_json")"
  SOURCE="$(jq -r '[.[] | select(startswith("source:")) | sub("^source:";"")] | first // "'"$SOURCE"'"' <<<"$tags_json")"
  TYPE="$(jq -r '[.[] | select(startswith("memory_type:")) | sub("^memory_type:";"")] | first // "'"$TYPE"'"' <<<"$tags_json")"
else
  [[ -n "$TITLE" ]] || { echo "--title is required for new retains" >&2; exit 2; }
  if [[ -n "$CONTENT_FILE" ]]; then CONTENT="$(cat "$CONTENT_FILE")"; else CONTENT="$(cat)"; fi
  [[ -n "$CONTENT" ]] || { echo "no content (stdin or --content-file)" >&2; exit 2; }
fi

# ---------- validation: same vocabulary the shipper enforces ----------
STRATEGY="$(strategy_for "$TYPE")" || { echo "unknown type '$TYPE'" >&2; exit 2; }
case "$SCOPE" in business|platform|repo) ;; *) echo "bad scope '$SCOPE'" >&2; exit 2 ;; esac
case "$SOURCE" in human|agent|external) ;; *) echo "bad source '$SOURCE'" >&2; exit 2 ;; esac
if command -v gc >/dev/null 2>&1 && [[ -n "$REPOS" ]]; then
  KNOWN="$(gc rig list --json 2>/dev/null | jq -r '.rigs[]?.name // empty' 2>/dev/null | sort -u || true)"
  if [[ -n "$KNOWN" ]]; then
    while IFS= read -r r; do
      [[ -z "$r" ]] && continue
      grep -qx "$r" <<<"$KNOWN" || echo "WARN: repo '$r' matches no rig in this city" >&2
    done < <(tr ',' '\n' <<<"$REPOS")
  fi
fi

# ---------- report line: owned by this script, reader-visible ----------
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
times_word="times"; [[ "$HIT_COUNT" -eq 1 ]] && times_word="time"
CONTENT="$(sed '/^Reported [0-9][0-9]* times\{0,1\} (last: .*).$/d' <<<"$CONTENT")"
CONTENT="$(printf '%s\n\nReported %s %s (last: %s).\n' "$(printf '%s' "$CONTENT" | sed -e 's/[[:space:]]*$//')" "$HIT_COUNT" "$times_word" "$NOW")"

# ---------- payload per retain-contract ----------
repos_json="$(jq -cRn --arg s "$REPOS" '$s | split(",") | map(select(length>0))')"
domains_json="$(jq -cRn --arg s "$DOMAINS" '$s | split(",") | map(select(length>0))')"
context="$TYPE: $TITLE"
dj="$(jq -r 'join(", ")' <<<"$domains_json")"; [[ -n "$dj" ]] && context+=" — domain $dj"
rj="$(jq -r 'join(", ")' <<<"$repos_json")";   [[ -n "$rj" ]] && context+=", repo $rj"
context+="; agent-learned, arbitrated by the archivist"

tags="$(jq -cn --arg scope "$SCOPE" --arg type "$TYPE" --arg source "$SOURCE" \
  --argjson repos "$repos_json" --argjson domains "$domains_json" '
  ["scope:\($scope)"] + ($repos | map("repo:\(.)")) + ($domains | map("domain:\(.)"))
  + ["memory_type:\($type)", "source:\($source)"]')"
oscopes="$(jq -cn --arg scope "$SCOPE" --argjson repos "$repos_json" --argjson domains "$domains_json" '
  ($domains | map(["domain:\(.)"])) + ($repos | map(["repo:\(.)"])) + [["scope:\($scope)"]]')"
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

# ---------- serialization: never race a pending operation ----------
deadline=$(( $(date +%s) + DRAIN_TIMEOUT ))
while :; do
  pending="$(hindsight -o json operation list "$BANK" 2>/dev/null \
    | jq '[.[] | select(.status == "pending" or .status == "processing")] | length' 2>/dev/null || echo 0)"
  [[ "$pending" -eq 0 ]] && break
  if [[ $(date +%s) -ge $deadline ]]; then
    echo "drain timeout: $pending operation(s) still in flight; NOT retaining $ID (racing a pending op orphans memories)" >&2
    exit 3
  fi
  sleep 5
done

resp="$(curl -sS -X POST "$API/v1/default/banks/$BANK/memories" \
  -H 'Content-Type: application/json' -d "$payload")"
op_id="$(jq -r '.operation_id // empty' <<<"$resp")"
[[ -n "$op_id" ]] || { echo "FAILED $ID: retain rejected: $resp" >&2; exit 1; }

for i in $(seq 1 90); do
  st="$(hindsight -o json operation get "$BANK" "$op_id" 2>/dev/null | jq -r '.status // "unknown"')"
  case "$st" in
    pending|processing) sleep 10 ;;
    completed) echo "RETAINED $ID op=$op_id hit_count=$HIT_COUNT"; exit 0 ;;
    *) err="$(hindsight -o json operation get "$BANK" "$op_id" 2>/dev/null | jq -r '.error_message // "n/a"')"
       echo "FAILED $ID: op $op_id ended '$st': $err" >&2; exit 1 ;;
  esac
done
echo "FAILED $ID: op $op_id did not reach terminal status" >&2
exit 1
