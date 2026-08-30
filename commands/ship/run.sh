#!/usr/bin/env bash
# gc command: ship — reconcile docs trees into the Hindsight memory bank.
#
# The same ship path the archivist's scheduled order uses, exposed for
# humans and formula steps. Safe to run at any time from anywhere in the
# city: over-shipping is harmless (idempotent document_id replace), and
# the underlying script serializes per document and refuses malformed
# frontmatter loudly.
#
#   ship                      # auto-resolve roots (rigs' docs/ + city docs/), ship
#   ship --dry-run            # validate + report what would ship, write nothing
#   ship path/to/docs         # ship specific roots only
#   ship --bank <id> ...      # target a non-default bank (e.g. a test bank)
set -euo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHIP="$PACK_DIR/assets/scripts/ship-docs.sh"

BANK_SET=false
PASS=()
ROOTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bank) BANK_SET=true; PASS+=("$1" "$2"); shift 2 ;;
    --api|--ref|--domains|--drain-timeout) PASS+=("$1" "$2"); shift 2 ;;
    --dry-run|--fetch) PASS+=("$1"); shift ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) ROOTS+=("$1"); shift ;;
  esac
done
$BANK_SET || PASS+=(--bank "${HINDSIGHT_BANK:-stacked-chips-v2}")

# No roots given: derive each rig's docs/ from the city, plus the city
# root's own docs/ if present.
if [[ ${#ROOTS[@]} -eq 0 ]]; then
  if command -v gc >/dev/null 2>&1; then
    while IFS= read -r path; do
      [[ -n "$path" && -d "$path/docs" ]] && ROOTS+=("$path/docs")
    done < <(gc rig list --json 2>/dev/null | jq -r '.rigs[]?.path // empty' 2>/dev/null || true)
  fi
  [[ -d "docs" ]] && ROOTS+=("docs")
fi
if [[ ${#ROOTS[@]} -eq 0 ]]; then
  echo "no docs roots found (no rigs with docs/, no ./docs); pass roots explicitly" >&2
  exit 2
fi

exec "$SHIP" "${PASS[@]}" "${ROOTS[@]}"
