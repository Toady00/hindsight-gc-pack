#!/usr/bin/env bash
# Read-only CLI adapter using the same connection as the pack's writers.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/common.sh"
API="" BANK="${HINDSIGHT_BANK:-}"
ARGS=("$@")
while [[ "${1:-}" == "-o" || "${1:-}" == "--output" ]]; do shift 2; done
case "${1:-} ${2:-}" in
  'memory recall'|'memory reflect'|'document get'|'document list'|'mental-model get'|'mental-model list'|'operation get'|'operation list'|'bank config'|'bank stats'|'tag list') ;;
  *) echo "hindsight-read.sh accepts read operations only" >&2; exit 2 ;;
esac
hs_connect
exec hindsight "${ARGS[@]}"
