#!/bin/bash
set -uo pipefail  # not -e: assertions must be allowed to fail and continue

_TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_TEST_DIR/test_helpers.sh"
source "$_TEST_DIR/../synapse-common.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- detect_target ---
detect_target
if [[ "$(uname -s)" == "Darwin" ]]; then
    assert_eq "detect_target on Darwin sets TARGET=mac" "mac" "$TARGET"
else
    assert_eq "detect_target on non-Darwin sets TARGET=qnap" "qnap" "$TARGET"
fi

# --- _env_var ---
cat > "$TMP/env1" <<'EOF'
FOO=bar
# a comment, not KEY=value
BAZ=qux with spaces
EOF
assert_eq "_env_var reads a simple key" "bar" "$(_env_var "$TMP/env1" FOO)"
assert_eq "_env_var reads a value with spaces" "qux with spaces" "$(_env_var "$TMP/env1" BAZ)"
assert_eq "_env_var returns empty for a missing key" "" "$(_env_var "$TMP/env1" MISSING)"
assert_eq "_env_var returns empty for a missing file" "" "$(_env_var "$TMP/does-not-exist" FOO)"

# --- _set_env_var ---
cp "$TMP/env1" "$TMP/env2"
_set_env_var "$TMP/env2" FOO "updated"
assert_eq "_set_env_var updates an existing key" "updated" "$(_env_var "$TMP/env2" FOO)"
assert_eq "_set_env_var leaves other keys alone" "qux with spaces" "$(_env_var "$TMP/env2" BAZ)"

_set_env_var "$TMP/env2" NEWKEY "newvalue"
assert_eq "_set_env_var appends a missing key" "newvalue" "$(_env_var "$TMP/env2" NEWKEY)"

# --- _detect_venv_dir ---
mkdir -p "$TMP/proj_venv/venv/bin"
touch "$TMP/proj_venv/venv/bin/python"
assert_eq "_detect_venv_dir finds venv/" "venv" "$(_detect_venv_dir "$TMP/proj_venv")"

mkdir -p "$TMP/proj_dotvenv/.venv/bin"
touch "$TMP/proj_dotvenv/.venv/bin/python"
assert_eq "_detect_venv_dir prefers .venv/ when both exist" ".venv" "$(_detect_venv_dir "$TMP/proj_dotvenv")"

mkdir -p "$TMP/proj_none"
assert_eq "_detect_venv_dir returns empty when neither exists" "" "$(_detect_venv_dir "$TMP/proj_none")"

test_summary
exit $?
