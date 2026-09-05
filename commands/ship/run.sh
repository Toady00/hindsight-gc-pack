#!/usr/bin/env bash
# gc command: ship — reconcile docs trees into the Hindsight memory bank.
#
# Writes from humans and other agents are queued to the managed archivist.
# A managed archivist executes immediately; --dry-run stays local.
# No shared lock is used. One city must own each writable bank.
#
#   ship                      # queue an automatic roots scan
#   ship --dry-run            # preview locally
#   ship path/to/docs         # queue a scan of specific roots
set -euo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHIP="$PACK_DIR/assets/scripts/ship-docs.sh"

source "$PACK_DIR/assets/scripts/common.sh"

# Formula requests contain a JSON argv encoded as base64, never shell source.
if [[ "${1:-}" == "--request" ]]; then
  hs_require_writer
  [[ $# -ge 2 ]] || { echo "--request requires a value" >&2; exit 2; }
  request="${2-}"
  shift 2
  if [[ -n "$request" ]]; then
    exec python3 "$PACK_DIR/assets/scripts/ship-request.py" execute "$request"
  fi
fi

BANK_SET=false
DRY_RUN=false
PASS=()
ROOTS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bank) BANK_SET=true; PASS+=("$1" "$2"); shift 2 ;;
    --api|--ref|--domains|--schema|--drain-timeout) PASS+=("$1" "$2"); shift 2 ;;
    --dry-run) DRY_RUN=true; PASS+=("$1"); shift ;;
    --fetch|--reprocess) PASS+=("$1"); shift ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) ROOTS+=("$1"); shift ;;
  esac
done
# Normalize explicit paths before queueing: the archivist has a different cwd.
if ! $DRY_RUN && ! hs_is_writer; then
  request="$(python3 "$PACK_DIR/assets/scripts/ship-request.py" encode "${PASS[@]}" -- "${ROOTS[@]}")"
  target="${HINDSIGHT_ARCHIVIST:-${GC_PACK_NAME:-hindsight}.archivist}"
  "${GC_BIN:-gc}" sling "$target" mol-hindsight-ship --formula --var "request=$request"
  echo "Queued ship request for $target. Completion and findings will be recorded on the formula bead."
  exit 0
fi

# The bank is never defaulted: each city declares its own, once, in
# [workspace] env. Managed sessions inherit it; humans running this
# outside a managed session export it or pass --bank.
if ! $BANK_SET; then
  if [[ -n "${HINDSIGHT_BANK:-}" ]]; then
    PASS+=(--bank "$HINDSIGHT_BANK")
  else
    echo "no bank: set HINDSIGHT_BANK in [workspace] env (or export it), or pass --bank <id>" >&2
    exit 2
  fi
fi

# Discovery happens after the shared scan record is started, so even registry
# failures are visible from the next machine. Git, not this checkout, decides
# whether each rig has published docs.
exec "$SHIP" "${PASS[@]}" "${ROOTS[@]}"
