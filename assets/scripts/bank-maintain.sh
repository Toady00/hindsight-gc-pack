#!/usr/bin/env bash
# bank-maintain.sh — deterministic bank maintenance: drain, consolidate,
# tag audit. The mechanical half of the nightly maintenance formula; the
# agent runs this, reads its report, and applies judgment only to what a
# script cannot decide (mental-model overreach, escalation).
#
#   bank-maintain.sh [options]
#
# Options:
#   --bank <id>            bank to maintain (default: $HINDSIGHT_BANK;
#                          no hardcoded fallback — every city declares its
#                          own bank in [workspace] env)
#   --api <url>            Hindsight API base (default: $HINDSIGHT_API, else ~/.hindsight/config api_url; never a hardcoded server)
#   --domains <file>       known-domains file, one per line (audit check d)
#   --schema <dir>         schema directory whose audit-vocab.json defines
#                          the tag axes and closed vocabularies (default:
#                          $HINDSIGHT_SCHEMA, else the pack's
#                          schemas/docs). Write-side dialect and
#                          audit vocabulary are one artifact — they cannot
#                          drift. A schema with no audit-vocab.json gets
#                          structural checks only (c/e).
#   --drain-timeout <sec>  max wait for in-flight operations (default 600)
#   --skip-consolidate     drain + audit only
#
# Exit codes (the formula branches on these):
#   0  clean — consolidated, audit found nothing
#   2  consolidated fine, but the tag audit has findings (report on stdout)
#   3  drain timeout — operations still in flight; consolidation was NOT run
#   4  consolidation failed even after one recover+retry
#
# Tag audit checks (all deterministic):
#   a. axis check      every tag starts with a schema-declared axis
#   b. vocabulary      values of closed axes are schema-legal
#   c. rig registry    repo: values exist in `gc rig list` (when available,
#                      and only if the schema marks the repo axis as the
#                      rig registry)
#   d. domain vocab    domain: values appear in --domains file (when given)
#   e. near-duplicates case-fold collisions and Levenshtein<=2 value pairs
#                      within the same axis (len>3, to skip short-value noise)
#
# The raw tag list lands at ${TMPDIR:-/tmp}/hindsight-tag-audit.<bank>.json —
# one stable, explicitly named file that each run overwrites (diffable
# between runs, reaped on reboot). All other scratch goes in a mktemp -d.

set -euo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$PACK_DIR/assets/scripts/common.sh"
API=""
BANK=""
DOMAINS_FILE=""
SCHEMA_DIR=""
DRAIN_TIMEOUT=600
SKIP_CONSOLIDATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bank) BANK="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    --domains) DOMAINS_FILE="$2"; shift 2 ;;
    --schema) SCHEMA_DIR="$2"; shift 2 ;;
    --drain-timeout) DRAIN_TIMEOUT="$2"; shift 2 ;;
    --skip-consolidate) SKIP_CONSOLIDATE=true; shift ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[[ -n "$BANK" ]] || BANK="${HINDSIGHT_BANK:-}"
[[ -n "$BANK" ]] || { echo "no bank: set HINDSIGHT_BANK in [workspace] env, or pass --bank <id>" >&2; exit 64; }
hs_connect
$SKIP_CONSOLIDATE || hs_require_writer
[[ "$DRAIN_TIMEOUT" =~ ^[0-9]+$ ]] || { echo "invalid drain timeout" >&2; exit 64; }

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -n "$SCHEMA_DIR" ]] || SCHEMA_DIR="${HINDSIGHT_SCHEMA:-$PACK_DIR/schemas/docs}"
VOCAB="$SCHEMA_DIR/audit-vocab.json"
[[ -f "$VOCAB" ]] || VOCAB=""

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
TAGFILE="${TMPDIR:-/tmp}/hindsight-tag-audit.$BANK.json"

# ---------- 1. drain ----------
echo "== drain =="
hs_drain || exit $?
echo "drained: no operations in flight"

# ---------- 2. consolidate ----------
if ! $SKIP_CONSOLIDATE; then
  echo "== consolidate =="
  if ! hindsight bank consolidate "$BANK" --wait; then
    echo "consolidation failed; attempting recover + one retry" >&2
    hindsight bank consolidation-recover "$BANK" || true
    if ! hindsight bank consolidate "$BANK" --wait; then
      echo "CONSOLIDATION FAILED after recover+retry" >&2
      exit 4
    fi
  fi
fi

# ---------- 3. config drift ----------
# Bulk loads may deliberately disable auto-consolidation, and a
# crashed load's trap never fires. This catches the drift within a day.
echo "== config drift =="
hindsight -o json bank config "$BANK" > "$TMP/cfg.json" 2>/dev/null || echo '{}' > "$TMP/cfg.json"
AUTOC="$(jq -r 'if .config | has("enable_auto_consolidation") then .config.enable_auto_consolidation else "unknown" end' "$TMP/cfg.json")"
if [[ "$AUTOC" != "true" ]]; then
  echo "CONFIG-DRIFT  enable_auto_consolidation=$AUTOC (expected true — a bulk load may have died before re-enabling it)" >> "$TMP/findings.txt.pre"
  echo "config drift: enable_auto_consolidation=$AUTOC"
else
  echo "config ok: enable_auto_consolidation=true"
fi

# ---------- 4. tag audit ----------
echo "== tag audit =="
echo '[]' > "$TMP/tag-items.json"
offset=0
while :; do
  hindsight -o json tag list "$BANK" --limit 500 --offset "$offset" > "$TMP/tags-page.json" || exit 5
  jq -e '.items | type == "array" and all(.[]; .tag | type == "string")' "$TMP/tags-page.json" >/dev/null || exit 5
  count="$(jq '.items | length' "$TMP/tags-page.json")"
  jq -s '.[0] + .[1].items' "$TMP/tag-items.json" "$TMP/tags-page.json" > "$TMP/tag-merged.json"
  mv "$TMP/tag-merged.json" "$TMP/tag-items.json"
  offset=$((offset + count))
  total="$(jq -r '.total // empty' "$TMP/tags-page.json")"
  if [[ -n "$total" ]]; then
    [[ "$offset" -ge "$total" ]] && break
    [[ "$count" -gt 0 ]] || { echo "incomplete tag inventory" >&2; exit 5; }
  elif [[ "$count" -lt 500 ]]; then break
  fi
done
jq '{items:., total:length}' "$TMP/tag-items.json" > "$TAGFILE"
jq -r '.items[].tag' "$TAGFILE" | sort -u > "$TMP/tags.txt"
: > "$TMP/findings.txt"
[[ -f "$TMP/findings.txt.pre" ]] && cat "$TMP/findings.txt.pre" >> "$TMP/findings.txt"

# a. axis check — axes come from the schema's audit vocab
if [[ -n "$VOCAB" ]]; then
  AXES_RE="$(jq -r '.axes | join("|")' "$VOCAB")"
  grep -Ev "^($AXES_RE):" "$TMP/tags.txt" \
    | sed 's/^/UNKNOWN-AXIS  /' >> "$TMP/findings.txt" || true
fi

# b. vocabulary check — closed axes come from the schema's audit vocab
if [[ -n "$VOCAB" ]]; then
  while IFS= read -r tag; do
    axis="${tag%%:*}"; val="${tag#*:}"
    allowed="$(jq -r --arg a "$axis" '.closed[$a] // empty | .[]' "$VOCAB")"
    [[ -z "$allowed" ]] && continue
    grep -qx "$val" <<<"$allowed" || echo "BAD-VALUE     $tag"
  done < "$TMP/tags.txt" >> "$TMP/findings.txt"
fi

# c. repo values against the rig registry (soft dependency; only when the
#    schema declares the repo axis maps to the rig registry)
if command -v gc >/dev/null 2>&1 \
  && [[ -n "$VOCAB" && "$(jq -r '.repo_axis_is_rig_registry // false' "$VOCAB")" == "true" ]]; then
  registry_ok=false
  if gc rig list --json > "$TMP/rigs.json" && jq -e '.rigs | type == "array" and all(.[]; .name | type == "string")' "$TMP/rigs.json" >/dev/null; then
    registry_ok=true
    jq -r '.rigs[].name' "$TMP/rigs.json" | sort -u > "$TMP/rigs.txt"
  else
    echo "AUDIT-INCOMPLETE rig registry unavailable" >> "$TMP/findings.txt"
    : > "$TMP/rigs.txt"
  fi
  if $registry_ok; then
    { grep '^repo:' "$TMP/tags.txt" || true; } | sed 's/^repo://' | while IFS= read -r r; do
      grep -Fxq "$r" "$TMP/rigs.txt" || echo "UNKNOWN-RIG   repo:$r"
    done >> "$TMP/findings.txt"
  fi
fi

# d. domain values against the vocab file (when given)
if [[ -n "$DOMAINS_FILE" && -f "$DOMAINS_FILE" ]]; then
  { grep '^domain:' "$TMP/tags.txt" || true; } | sed 's/^domain://' | while IFS= read -r d; do
    grep -Fxq "$d" "$DOMAINS_FILE" || echo "NEW-DOMAIN    domain:$d"
  done >> "$TMP/findings.txt"
fi

# e. near-duplicates within each axis: case-fold collisions + Levenshtein<=2
awk -F: '
function lev(a, b,   la, lb, i, j, c, d) {
  la = length(a); lb = length(b)
  for (i = 0; i <= la; i++) d[i, 0] = i
  for (j = 0; j <= lb; j++) d[0, j] = j
  for (i = 1; i <= la; i++)
    for (j = 1; j <= lb; j++) {
      c = (substr(a, i, 1) == substr(b, j, 1)) ? 0 : 1
      d[i, j] = d[i-1, j] + 1
      if (d[i, j-1] + 1 < d[i, j]) d[i, j] = d[i, j-1] + 1
      if (d[i-1, j-1] + c < d[i, j]) d[i, j] = d[i-1, j-1] + c
    }
  return d[la, lb]
}
{ axis = $1; val = substr($0, length(axis) + 2); vals[axis, ++n[axis]] = val }
END {
  for (axis in n)
    for (i = 1; i <= n[axis]; i++)
      for (j = i + 1; j <= n[axis]; j++) {
        a = vals[axis, i]; b = vals[axis, j]
        if (tolower(a) == tolower(b))
          printf "CASE-SPLIT    %s:%s ~ %s:%s\n", axis, a, axis, b
        else if (length(a) > 3 && length(b) > 3 && lev(tolower(a), tolower(b)) <= 2)
          printf "NEAR-DUP      %s:%s ~ %s:%s\n", axis, a, axis, b
      }
}' "$TMP/tags.txt" >> "$TMP/findings.txt"

# ---------- report ----------
FINDINGS=$(grep -c . "$TMP/findings.txt" || true)
echo "tag list: $TAGFILE"
if [[ "$FINDINGS" -gt 0 ]]; then
  echo "AUDIT FINDINGS ($FINDINGS):"
  sort -u "$TMP/findings.txt"
  echo "---"
  echo "drain=ok consolidate=$($SKIP_CONSOLIDATE && echo skipped || echo ok) audit_findings=$FINDINGS"
  exit 2
fi
echo "---"
echo "drain=ok consolidate=$($SKIP_CONSOLIDATE && echo skipped || echo ok) audit_findings=0"
