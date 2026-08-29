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

# --- _ensure_qnap_runtime_env_defaults ---
RUNTIME_ENV="$TMP/runtime.env"
cat > "$RUNTIME_ENV" <<'EOF'
TELEGRAM_BOT_TOKEN=abc123
CLAUDE_CMD=/opt/homebrew/bin/claude
EOF
_ensure_qnap_runtime_env_defaults "$RUNTIME_ENV"

assert_eq "existing unrelated key is preserved" "abc123" "$(_env_var "$RUNTIME_ENV" TELEGRAM_BOT_TOKEN)"
assert_eq "existing CLAUDE_CMD is overridden to the container path" "/usr/local/bin/claude" "$(_env_var "$RUNTIME_ENV" CLAUDE_CMD)"
assert_eq "VAULT_PATH set to the container mount point" "/app/vault" "$(_env_var "$RUNTIME_ENV" VAULT_PATH)"
assert_eq "AGY_CMD set to the container path" "/home/synapse/.local/bin/agy" "$(_env_var "$RUNTIME_ENV" AGY_CMD)"
assert_eq "SESSION_STORAGE_PATH set to the container path" "/app/data/sessions.json" "$(_env_var "$RUNTIME_ENV" SESSION_STORAGE_PATH)"
assert_eq "REMINDERS_JSON_PATH set to the container path" "/app/vault/reminders/reminders.json" "$(_env_var "$RUNTIME_ENV" REMINDERS_JSON_PATH)"

LINES_BEFORE="$(wc -l < "$RUNTIME_ENV" | tr -d ' ')"
_ensure_qnap_runtime_env_defaults "$RUNTIME_ENV"
LINES_AFTER="$(wc -l < "$RUNTIME_ENV" | tr -d ' ')"
assert_eq "re-running adds no duplicate lines" "$LINES_BEFORE" "$LINES_AFTER"

# --- _ensure_qnap_host_dirs ---
chown() { return 1; }  # no "synapse" user on the test machine — must be fail-soft, not fatal
_ensure_qnap_host_dirs
for d in credentials/claude credentials/gemini credentials/etrade credentials/amazon data; do
    assert_eq "_ensure_qnap_host_dirs creates $d" "true" "$([ -d "$SYNAPSE_HOST_DIR/$d" ] && echo true || echo false)"
done
unset -f chown

# --- _qnap_vault_clone_own ---
OWN_VAULT_DIR="$TMP/own_vault"

# Refuses to overwrite an existing directory, without touching docker.
mkdir -p "$TMP/own_vault_exists"
docker() { echo "DOCKER_SHOULD_NOT_RUN"; return 0; }
if _qnap_vault_clone_own "$TMP/own_vault_exists" "git@github.com:someone/vault.git" 2>/dev/null; then
    assert_eq "_qnap_vault_clone_own refuses an existing directory" "refused" "did not refuse"
else
    assert_eq "_qnap_vault_clone_own refuses an existing directory" "refused" "refused"
fi
unset -f docker

# Refuses to run without the SSH deploy key present, without touching docker.
NO_KEY_HOST_DIR="$TMP/no_key_host_dir"
mkdir -p "$NO_KEY_HOST_DIR"
docker() { echo "DOCKER_SHOULD_NOT_RUN"; return 0; }
if (SYNAPSE_HOST_DIR="$NO_KEY_HOST_DIR"; _qnap_vault_clone_own "$NO_KEY_HOST_DIR/vault" "git@github.com:someone/vault.git") 2>/dev/null; then
    assert_eq "_qnap_vault_clone_own refuses without an SSH key present" "refused" "did not refuse"
else
    assert_eq "_qnap_vault_clone_own refuses without an SSH key present" "refused" "refused"
fi
unset -f docker

# Builds the expected docker invocation: mounts the SSH key and clones the
# given URL (not the template's) into the vault dir's parent, by name.
CAPTURE_FILE_OWN="$TMP/captured_own_clone_script"
docker() {
    local prev="" a mount_args=""
    for a in "$@"; do
        [ "$prev" = "-c" ] && printf '%s' "$a" > "$CAPTURE_FILE_OWN"
        [ "$prev" = "-v" ] && mount_args="$mount_args|$a"
        prev="$a"
    done
    printf '%s' "$mount_args" >> "$TMP/captured_own_clone_mounts"
    return 0
}
chown() { return 0; }
_qnap_vault_clone_own "$OWN_VAULT_DIR" "git@github.com:someone/vault.git" > /dev/null 2>&1
CAPTURED_OWN="$(cat "$CAPTURE_FILE_OWN")"
assert_contains "clones the given URL, not the template's" "$CAPTURED_OWN" "git clone 'git@github.com:someone/vault.git' 'own_vault'"
assert_contains "mounts the SSH deploy key" "$(cat "$TMP/captured_own_clone_mounts")" "$SYNAPSE_HOST_DIR/ssh/id_ed25519:/root/.ssh/id_ed25519"
unset -f docker chown

# --- _ensure_qnap_timezone ---

# Already set: prompt is skipped, existing value untouched. Feeding a
# non-empty stdin value here would fail the test if the function read it
# anyway, since _set_env_var isn't idempotent-checked separately.
TZ_ENV="$TMP/tz_compose.env"
echo "TZ=America/New_York" > "$TZ_ENV"
(
    COMPOSE_ENV_FILE="$TZ_ENV"
    _ensure_qnap_timezone <<< "Europe/London"
)
assert_eq "existing TZ is left alone, prompt skipped" "America/New_York" "$(_env_var "$TZ_ENV" TZ)"

# Not set, user provides a value: gets written.
TZ_ENV_UNSET="$TMP/tz_compose_unset.env"
touch "$TZ_ENV_UNSET"
(
    COMPOSE_ENV_FILE="$TZ_ENV_UNSET"
    _ensure_qnap_timezone <<< "America/Los_Angeles"
)
assert_eq "TZ set from prompt answer" "America/Los_Angeles" "$(_env_var "$TZ_ENV_UNSET" TZ)"

# Not set, blank answer (or non-TTY EOF): stays unset, no error — UTC
# fallback in docker-compose.yml handles it, and setup must not abort.
TZ_ENV_BLANK="$TMP/tz_compose_blank.env"
touch "$TZ_ENV_BLANK"
BLANK_RESULT="$(
    set -e
    (
        COMPOSE_ENV_FILE="$TZ_ENV_BLANK"
        _ensure_qnap_timezone < /dev/null
    )
    echo "COMPLETED"
)"
assert_eq "blank/EOF answer completes without error" "COMPLETED" "$BLANK_RESULT"
assert_eq "blank/EOF answer leaves TZ unset" "" "$(_env_var "$TZ_ENV_BLANK" TZ)"

# --- _ensure_qnap_git_identity ---

# Both already set: prompt is skipped entirely, no stdin read at all —
# feeding answers here would fail the test if they got written anyway.
GIT_ENV_BOTH_SET="$TMP/git_compose_both_set.env"
cat > "$GIT_ENV_BOTH_SET" <<'EOF'
GIT_USER_NAME=Existing Name
GIT_USER_EMAIL=existing@example.com
EOF
(
    COMPOSE_ENV_FILE="$GIT_ENV_BOTH_SET"
    _ensure_qnap_git_identity <<< "Someone Else
someone@else.com"
)
assert_eq "existing GIT_USER_NAME is left alone" "Existing Name" "$(_env_var "$GIT_ENV_BOTH_SET" GIT_USER_NAME)"
assert_eq "existing GIT_USER_EMAIL is left alone" "existing@example.com" "$(_env_var "$GIT_ENV_BOTH_SET" GIT_USER_EMAIL)"

# Neither set: prompts for both, in order, and writes both answers.
GIT_ENV_UNSET="$TMP/git_compose_unset.env"
touch "$GIT_ENV_UNSET"
(
    COMPOSE_ENV_FILE="$GIT_ENV_UNSET"
    _ensure_qnap_git_identity <<< "Jane Doe
jane@example.com"
)
assert_eq "GIT_USER_NAME set from first prompt answer" "Jane Doe" "$(_env_var "$GIT_ENV_UNSET" GIT_USER_NAME)"
assert_eq "GIT_USER_EMAIL set from second prompt answer" "jane@example.com" "$(_env_var "$GIT_ENV_UNSET" GIT_USER_EMAIL)"

# Only one missing: prompts for just that one, existing value untouched.
GIT_ENV_EMAIL_ONLY="$TMP/git_compose_email_only.env"
echo "GIT_USER_NAME=Already Set" > "$GIT_ENV_EMAIL_ONLY"
(
    COMPOSE_ENV_FILE="$GIT_ENV_EMAIL_ONLY"
    _ensure_qnap_git_identity <<< "new@example.com"
)
assert_eq "existing GIT_USER_NAME untouched when only email is missing" "Already Set" "$(_env_var "$GIT_ENV_EMAIL_ONLY" GIT_USER_NAME)"
assert_eq "GIT_USER_EMAIL set from the one prompt asked" "new@example.com" "$(_env_var "$GIT_ENV_EMAIL_ONLY" GIT_USER_EMAIL)"

# Blank/EOF answers: stays unset, no error — docker-compose.yml's
# ${VAR:?...} catches it later at build time; setup itself must not abort.
GIT_ENV_BLANK="$TMP/git_compose_blank.env"
touch "$GIT_ENV_BLANK"
GIT_BLANK_RESULT="$(
    set -e
    (
        COMPOSE_ENV_FILE="$GIT_ENV_BLANK"
        _ensure_qnap_git_identity < /dev/null > /dev/null
    )
    echo "COMPLETED"
)"
assert_eq "blank/EOF answers complete without error" "COMPLETED" "$GIT_BLANK_RESULT"
assert_eq "blank/EOF answers leave GIT_USER_NAME unset" "" "$(_env_var "$GIT_ENV_BLANK" GIT_USER_NAME)"
assert_eq "blank/EOF answers leave GIT_USER_EMAIL unset" "" "$(_env_var "$GIT_ENV_BLANK" GIT_USER_EMAIL)"

test_summary
exit $?
