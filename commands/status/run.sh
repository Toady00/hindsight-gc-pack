#!/usr/bin/env bash
# Read persistent full-scan health. Exit 1 means missing, stale, or incomplete.
set -euo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$PACK_DIR/assets/scripts/common.sh"
BANK="${HINDSIGHT_BANK:-}" API=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bank) BANK="$2"; shift 2 ;;
    --api) API="$2"; shift 2 ;;
    -h|--help) echo 'status [--bank <id>] [--api <url>]'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$BANK" ]] || { echo 'set HINDSIGHT_BANK or pass --bank' >&2; exit 2; }
hs_connect
exec python3 "$PACK_DIR/assets/scripts/ship-report.py" status "$API" "$BANK"
