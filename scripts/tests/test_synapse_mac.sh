#!/bin/bash
set -uo pipefail

_TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_TEST_DIR/test_helpers.sh"
source "$_TEST_DIR/../synapse-common.sh"

# PROJECT_DIR points at the real repo root so _generate_mac_plist finds the
# real template file — this is read-only, so it's safe to use directly.
PROJECT_DIR="$(cd "$_TEST_DIR/../.." && pwd)"
source "$_TEST_DIR/../synapse-mac.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- _generate_mac_plist ---
OUT="$TMP/generated.plist"
_generate_mac_plist "$OUT"

CONTENT="$(cat "$OUT")"
assert_not_contains "generated plist has no leftover __PROJECT_DIR__ placeholder" "$CONTENT" "__PROJECT_DIR__"
assert_not_contains "generated plist has no leftover __NODE_BIN__ placeholder" "$CONTENT" "__NODE_BIN__"
assert_not_contains "generated plist has no leftover __VENV_DIR__ placeholder" "$CONTENT" "__VENV_DIR__"
assert_contains "generated plist references the real PROJECT_DIR" "$CONTENT" "$PROJECT_DIR"
assert_contains "generated plist points at services.ingestion.main" "$CONTENT" "services.ingestion.main"

# --- constants ---
assert_eq "PLIST_NAME is the expected filename" "com.synapse.ingestion.plist" "$PLIST_NAME"
assert_eq "LABEL matches the plist's launchd label" "com.synapse.ingestion" "$LABEL"

test_summary
exit $?
