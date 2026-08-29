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
cd - > /dev/null

test_summary
exit $?
