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

# --- metacharacter handling ---
# Test values with sed/regex metacharacters
mkdir -p "$TMP/meta"
_set_env_var "$TMP/meta/env3" KEY_PATH "/path/with/slashes"
assert_eq "_set_env_var handles value with slashes" "/path/with/slashes" "$(_env_var "$TMP/meta/env3" KEY_PATH)"

_set_env_var "$TMP/meta/env3" KEY_AMP "value&with&ampersand"
assert_eq "_set_env_var handles value with ampersands" "value&with&ampersand" "$(_env_var "$TMP/meta/env3" KEY_AMP)"

_set_env_var "$TMP/meta/env3" KEY_DOLLAR 'path$with$dollar'
assert_eq "_set_env_var handles value with dollar signs" 'path$with$dollar' "$(_env_var "$TMP/meta/env3" KEY_DOLLAR)"

# Update an existing value with metacharacters
_set_env_var "$TMP/meta/env3" KEY_PATH "/updated/path&with&both"
assert_eq "_set_env_var updates value with metacharacters" "/updated/path&with&both" "$(_env_var "$TMP/meta/env3" KEY_PATH)"

# Test keys with regex metacharacters
_set_env_var "$TMP/meta/env4" "KEY.WITH.DOTS" "value1"
assert_eq "_env_var reads key with dots" "value1" "$(_env_var "$TMP/meta/env4" "KEY.WITH.DOTS")"

_set_env_var "$TMP/meta/env4" "KEY[BRACKETS]" "value2"
assert_eq "_env_var reads key with brackets" "value2" "$(_env_var "$TMP/meta/env4" "KEY[BRACKETS]")"

_set_env_var "$TMP/meta/env4" "KEY*STAR" "value3"
assert_eq "_env_var reads key with asterisk" "value3" "$(_env_var "$TMP/meta/env4" "KEY*STAR")"

# --- Regression: _env_var must strip a matched pair of surrounding quotes.
# `KEY="value"` is legal in a .env and is what docker compose's own dotenv
# parser (and the old `set -a; source .env`) unquotes. Returning the literal
# quotes broke every downstream use of the value as a path (e.g. the -v
# mount flags built from SYNAPSE_HOST_DIR). ---
cat > "$TMP/env_quoted" <<'EOF'
DQ="/share/vol/synapse"
SQ='/share/vol/other'
PLAIN=/share/vol/plain
INNER=say "hi" there
UNMATCHED_LEAD="/share/vol/oops
UNMATCHED_TRAIL=/share/vol/oops"
MISMATCHED="/share/vol/mixed'
EMPTY_DQ=""
EOF
assert_eq "_env_var strips surrounding double quotes" "/share/vol/synapse" "$(_env_var "$TMP/env_quoted" DQ)"
assert_eq "_env_var strips surrounding single quotes" "/share/vol/other" "$(_env_var "$TMP/env_quoted" SQ)"
assert_eq "_env_var leaves an unquoted value alone" "/share/vol/plain" "$(_env_var "$TMP/env_quoted" PLAIN)"
assert_eq "_env_var keeps quotes that aren't surrounding" 'say "hi" there' "$(_env_var "$TMP/env_quoted" INNER)"
assert_eq "_env_var keeps an unmatched leading quote" '"/share/vol/oops' "$(_env_var "$TMP/env_quoted" UNMATCHED_LEAD)"
assert_eq "_env_var keeps an unmatched trailing quote" '/share/vol/oops"' "$(_env_var "$TMP/env_quoted" UNMATCHED_TRAIL)"
assert_eq "_env_var keeps mismatched quote characters" $'"/share/vol/mixed\'' "$(_env_var "$TMP/env_quoted" MISMATCHED)"
assert_eq "_env_var unquotes an empty quoted value to empty" "" "$(_env_var "$TMP/env_quoted" EMPTY_DQ)"

# --- Regression: _set_env_var must not corrupt values containing "=".
# The old implementation split the whole line on every "=" (FS/OFS "=") and
# assigned $2, which replaced only the first field and re-joined the rest —
# updating TOKEN=abc=def== to xyz=123== produced TOKEN=xyz=123===def==. ---
cat > "$TMP/env_eq" <<'EOF'
TOKEN=abc=def==
AFTER=untouched
EOF
assert_eq "_env_var reads a value containing =" "abc=def==" "$(_env_var "$TMP/env_eq" TOKEN)"
_set_env_var "$TMP/env_eq" TOKEN "xyz=123=="
assert_eq "_set_env_var replaces the whole value when the old value contained =" "xyz=123==" "$(_env_var "$TMP/env_eq" TOKEN)"
assert_eq "_set_env_var leaves the following key intact" "untouched" "$(_env_var "$TMP/env_eq" AFTER)"
assert_eq "_set_env_var did not grow the file" "2" "$(wc -l < "$TMP/env_eq" | tr -d ' ')"

_set_env_var "$TMP/env_eq" TOKEN "no-equals-now"
assert_eq "_set_env_var can shrink an =-containing value to one without" "no-equals-now" "$(_env_var "$TMP/env_eq" TOKEN)"

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
