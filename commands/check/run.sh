#!/usr/bin/env bash
# Read-only document check for one repository; no bank, Beads, or writer needed.
set -euo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$PACK_DIR/assets/scripts/check_docs.py" "$@"
