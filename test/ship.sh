#!/usr/bin/env bash
# Run fixture docs through the production writer, in the managed archivist.
# Test banks must be explicit and named hindsight-eval-*. No server default.
set -euo pipefail
BANK="${1:?usage: ship.sh hindsight-eval-<unique-id>}"
case "$BANK" in hindsight-eval-?*) ;; *) echo 'use an explicit hindsight-eval-* bank' >&2; exit 2 ;; esac
PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PACK_DIR/assets/scripts/common.sh"
hs_require_writer
ROOT_TEMP="$(mktemp -d)"
trap 'rm -rf "$ROOT_TEMP"' EXIT
git init --bare --initial-branch=main "$ROOT_TEMP/origin.git"
git clone "$ROOT_TEMP/origin.git" "$ROOT_TEMP/checkout"
mkdir "$ROOT_TEMP/checkout/docs"
cp "$PACK_DIR"/test/docs/*.md "$ROOT_TEMP/checkout/docs/"
git -C "$ROOT_TEMP/checkout" add -- docs
# Only this generated fixture commit opts out of signing by default.
# Set HINDSIGHT_TEST_COMMIT_GPGSIGN=true to use the configured signing key.
git -C "$ROOT_TEMP/checkout" -c core.hooksPath=/dev/null \
  -c user.name='Hindsight fixture' -c user.email='fixture@example.invalid' \
  -c commit.gpgsign="${HINDSIGHT_TEST_COMMIT_GPGSIGN:-false}" \
  commit -m 'Publish evaluation fixtures'
git -C "$ROOT_TEMP/checkout" -c core.hooksPath=/dev/null push origin main
"$PACK_DIR/assets/scripts/ship-docs.sh" --bank "$BANK" "$ROOT_TEMP/checkout/docs"
