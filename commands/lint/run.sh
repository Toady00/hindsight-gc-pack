#!/usr/bin/env bash
# Optional semantic lint experiment; no writer session or bank connection needed.
set -euo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 "$PACK_DIR/assets/scripts/semantic_lint.py" "$@"
