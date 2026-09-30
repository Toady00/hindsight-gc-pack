#!/usr/bin/env bash
# Read-only CLI adapter using the same connection as the pack's writers.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/common.sh"
API="" BANK="${HINDSIGHT_BANK:-}"
ARGS=("$@")
EXPLICIT_OUTPUT=0
while [[ "${1:-}" == "-o" || "${1:-}" == "--output" ]]; do
  EXPLICIT_OUTPUT=1
  shift 2
done
case "${1:-} ${2:-}" in
  'memory recall'|'memory reflect'|'document get'|'document list'|'mental-model get'|'mental-model list'|'operation get'|'operation list'|'bank config'|'bank stats'|'tag list') ;;
  *) echo "hindsight-read.sh accepts read operations only" >&2; exit 2 ;;
esac
# Older CLIs animate pretty-mode progress on stdout even when it is captured.
# Structured output skips that spinner without filtering answers or errors.
if [[ ! -t 1 && "$EXPLICIT_OUTPUT" == 0 ]]; then
  ARGS=(-o json "${ARGS[@]}")
fi
hs_connect
exec hindsight "${ARGS[@]}"
