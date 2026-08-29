#!/bin/bash
set -uo pipefail

_TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_TEST_DIR/test_helpers.sh"
source "$_TEST_DIR/../synapse-common.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# PROJECT_DIR must exist before synapse-qnap.sh is sourced (it reads
# PROJECT_DIR/.env for SYNAPSE_HOST_DIR at source time). Point it at a fake
# checkout so the test never touches the real repo-root .env.
PROJECT_DIR="$TMP/fake_project"
mkdir -p "$PROJECT_DIR"
echo "SYNAPSE_HOST_DIR=$TMP/fake_host_dir" > "$PROJECT_DIR/.env"

source "$_TEST_DIR/../synapse-qnap.sh"

# --- SYNAPSE_HOST_DIR / APP_ENV_FILE resolution ---
assert_eq "SYNAPSE_HOST_DIR is read from the repo-root .env" "$TMP/fake_host_dir" "$SYNAPSE_HOST_DIR"
assert_eq "APP_ENV_FILE is SYNAPSE_HOST_DIR/.env" "$TMP/fake_host_dir/.env" "$APP_ENV_FILE"
assert_eq "COMPOSE_ENV_FILE is the repo-root .env" "$PROJECT_DIR/.env" "$COMPOSE_ENV_FILE"

# --- _diff_needs_rebuild ---
REPO="$TMP/fake_repo"
mkdir -p "$REPO"
(
    cd "$REPO"
    git init -q
    git config user.email test@example.com
    git config user.name Test
    echo "hello" > app.py
    git add app.py
    git commit -q -m "initial"
    BEFORE="$(git rev-parse HEAD)"

    echo "changed" > app.py
    git add app.py
    git commit -q -m "code only change"
    AFTER_CODE_ONLY="$(git rev-parse HEAD)"

    echo "FROM python:3.12" > Dockerfile
    git add Dockerfile
    git commit -q -m "touch Dockerfile"
    AFTER_DOCKERFILE="$(git rev-parse HEAD)"

    echo "$BEFORE" > "$TMP/before_sha"
    echo "$AFTER_CODE_ONLY" > "$TMP/after_code_only_sha"
    echo "$AFTER_DOCKERFILE" > "$TMP/after_dockerfile_sha"
)

cd "$REPO"
if _diff_needs_rebuild "$(cat "$TMP/before_sha")" "$(cat "$TMP/after_code_only_sha")"; then
    assert_eq "_diff_needs_rebuild is false for a code-only change" "false" "true"
else
    assert_eq "_diff_needs_rebuild is false for a code-only change" "false" "false"
fi

if _diff_needs_rebuild "$(cat "$TMP/before_sha")" "$(cat "$TMP/after_dockerfile_sha")"; then
    assert_eq "_diff_needs_rebuild is true when Dockerfile changed" "true" "true"
else
    assert_eq "_diff_needs_rebuild is true when Dockerfile changed" "true" "false"
fi

# --- Regression: the rebuild-trigger pattern must have a single source of
# truth. It used to be hardcoded twice — once in _diff_needs_rebuild (the
# only copy under test) and once, by hand, inside _qnap_git_pull's embedded
# container script (the copy that actually ships and runs). The two could
# silently diverge, so the tested behaviour said nothing about the shipped
# one. These tests pin both to $_REBUILD_TRIGGER_REGEX. ---
assert_eq "_REBUILD_TRIGGER_REGEX holds the rebuild-trigger pattern" \
    '^(Dockerfile|requirements\.txt)$' "$_REBUILD_TRIGGER_REGEX"

# _diff_needs_rebuild must READ the variable, not a hardcoded pattern:
# repoint it at app.py and the previously code-only diff must now trigger.
_ORIG_REBUILD_REGEX="$_REBUILD_TRIGGER_REGEX"
_REBUILD_TRIGGER_REGEX='^app\.py$'
if _diff_needs_rebuild "$(cat "$TMP/before_sha")" "$(cat "$TMP/after_code_only_sha")"; then
    assert_eq "_diff_needs_rebuild honours \$_REBUILD_TRIGGER_REGEX" "true" "true"
else
    assert_eq "_diff_needs_rebuild honours \$_REBUILD_TRIGGER_REGEX" "true" "false"
fi
_REBUILD_TRIGGER_REGEX="$_ORIG_REBUILD_REGEX"
cd - > /dev/null

# --- The shipped container script must interpolate the same variable.
# Stub docker/chown and capture the `sh -c` script string _qnap_git_pull
# builds. The stub writes to a file because _qnap_git_pull runs docker
# inside a command substitution (a subshell — a variable assignment there
# would be lost). ---
mkdir -p "$SYNAPSE_HOST_DIR/ssh"
touch "$SYNAPSE_HOST_DIR/ssh/id_ed25519"
CAPTURE_FILE="$TMP/captured_container_script"

docker() {
    local prev="" a
    for a in "$@"; do
        [ "$prev" = "-c" ] && printf '%s' "$a" > "$CAPTURE_FILE"
        prev="$a"
    done
    return 0
}
chown() { return 0; }

_qnap_git_pull > /dev/null 2>&1
CAPTURED="$(cat "$CAPTURE_FILE")"
assert_contains "container script greps with the shared regex, correctly single-quoted" \
    "$CAPTURED" "grep -qE '^(Dockerfile|requirements\.txt)\$'"

# The container's /bin/sh must be able to re-parse the whole string, and
# must hand grep the regex as exactly ONE argument (the quoting is the easy
# part to get wrong when interpolating).
if printf '%s' "$CAPTURED" | sh -n 2>/dev/null; then
    assert_eq "container script is valid POSIX sh" "valid" "valid"
else
    assert_eq "container script is valid POSIX sh" "valid" "invalid"
fi

GREP_LINE="$(printf '%s\n' "$CAPTURED" | grep -o "grep -qE .*; then" | sed 's/; then$//')"
mkdir -p "$TMP/stubbin"
cat > "$TMP/stubbin/grep" <<'STUB'
#!/bin/sh
for a in "$@"; do echo "$a"; done
STUB
chmod +x "$TMP/stubbin/grep"
GREP_ARGV="$(PATH="$TMP/stubbin:$PATH" sh -c "$GREP_LINE")"
assert_eq "grep receives the regex as a single argument" \
    $'-qE\n^(Dockerfile|requirements\\.txt)$' "$GREP_ARGV"

# And it really comes from the variable, not a second hardcoded copy.
_REBUILD_TRIGGER_REGEX='^sentinel\.txt$'
_qnap_git_pull > /dev/null 2>&1
CAPTURED_ALT="$(cat "$CAPTURE_FILE")"
assert_contains "container script tracks changes to \$_REBUILD_TRIGGER_REGEX" \
    "$CAPTURED_ALT" "grep -qE '^sentinel\.txt\$'"
_REBUILD_TRIGGER_REGEX="$_ORIG_REBUILD_REGEX"

test_summary
exit $?
