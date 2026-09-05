#!/usr/bin/env bash
# Delegate to the shared read-only allowlist and connection adapter.
set -euo pipefail
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "$PACK_DIR/assets/scripts/hindsight-read.sh" "$@"
