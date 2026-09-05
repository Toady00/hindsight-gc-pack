#!/usr/bin/env bash
# Reconcile fetched Git snapshots through the archivist's shared Beads ledger.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$DIR/ship_docs.py" "$@"
