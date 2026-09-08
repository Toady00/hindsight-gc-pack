#!/usr/bin/env bash
# gc command: survey — the mechanical steps of a current-state survey.
#
# Thin wrapper over assets/scripts/survey.sh so the surveyor's formula steps
# and humans share one path: `gc <binding> survey <subcommand> <root-bead-id>`.
#
#   survey prepare <root> [--output-file FILE] [--publish pr|direct|none] [--pr-tool auto|gh|glab]
#   survey show    <root>
#   survey stamp   <root>
#   survey publish <root> [--body-file F]
#   survey cleanup <root> [--force]
set -euo pipefail

PACK_DIR="${GC_PACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
exec "$PACK_DIR/assets/scripts/survey.sh" "$@"
