#!/usr/bin/env bash
# Keep retain validation and writer admission in the existing helper.
set -euo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$PACK_DIR/assets/scripts/memory-retain.sh" "$@"
