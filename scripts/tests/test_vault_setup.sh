#!/bin/bash
set -uo pipefail

_TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_TEST_DIR/test_helpers.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Point PROJECT_DIR somewhere harmless before sourcing (synapse-qnap.sh
# reads PROJECT_DIR/.env at source time; we're only testing the Mac path
# here, which doesn't need it, but the file must still exist to source
# cleanly).
PROJECT_DIR="$TMP/unused_project"
mkdir -p "$PROJECT_DIR"
touch "$PROJECT_DIR/.env"

source "$_TEST_DIR/../synapse-common.sh"
source "$_TEST_DIR/../synapse-mac.sh"

# --- Build a fake "synapse-vault" template repo, standing in for the real
# public one so this test needs no network access. Its setup.sh is a
# non-interactive stand-in for the real (interactive) one. ---
FAKE_TEMPLATE="$TMP/fake-synapse-vault"
mkdir -p "$FAKE_TEMPLATE"
(
    cd "$FAKE_TEMPLATE"
    git init -q
    git config user.email test@example.com
    git config user.name Test
    echo "# fake template" > README.md
    cat > setup.sh <<'EOF'
#!/bin/bash
echo "personalized" > PERSONAL.md
EOF
    chmod +x setup.sh
    git add -A
    git commit -q -m "template initial commit"
)

# Give the clone target its own git identity too (Mac's _mac_vault_git just
# runs plain `git`, which needs user.name/user.email configured — set it
# globally for this test process only).
export HOME="$TMP/fake_home"
mkdir -p "$HOME"
git config --global user.email "vault-owner@example.com"
git config --global user.name "Vault Owner"

SYNAPSE_VAULT_TEMPLATE_URL="$FAKE_TEMPLATE"

VAULT_DIR="$TMP/vault"
_setup_vault "$VAULT_DIR" _mac_vault_clone _mac_vault_git _mac_vault_push <<< ""  # blank stdin: skip the optional remote prompt

assert_eq "vault dir was created" "true" "$([ -d "$VAULT_DIR" ] && echo true || echo false)"
assert_eq "vault dir is its own git repo (not the template's)" "true" "$([ -d "$VAULT_DIR/.git" ] && echo true || echo false)"
assert_eq "template's setup.sh ran (PERSONAL.md written)" "personalized" "$(cat "$VAULT_DIR/PERSONAL.md" 2>/dev/null)"

TEMPLATE_FIRST_COMMIT="$(git -C "$FAKE_TEMPLATE" rev-list --max-parents=0 HEAD)"
VAULT_FIRST_COMMIT="$(git -C "$VAULT_DIR" rev-list --max-parents=0 HEAD)"
assert_eq "vault's first commit differs from the template's (independent history)" "true" "$([ "$TEMPLATE_FIRST_COMMIT" != "$VAULT_FIRST_COMMIT" ] && echo true || echo false)"

VAULT_LOG_COUNT="$(git -C "$VAULT_DIR" log --oneline | wc -l | tr -d ' ')"
assert_eq "vault has exactly one commit (template history detached)" "1" "$VAULT_LOG_COUNT"

DIRTY="$(git -C "$VAULT_DIR" status --porcelain)"
assert_eq "vault has no uncommitted changes after setup" "" "$DIRTY"

# --- Refuses to overwrite an existing directory ---
mkdir -p "$TMP/already-exists"
if _setup_vault "$TMP/already-exists" _mac_vault_clone _mac_vault_git _mac_vault_push <<< "" 2>/dev/null; then
    assert_eq "_setup_vault refuses an existing directory" "refused" "did not refuse"
else
    assert_eq "_setup_vault refuses an existing directory" "refused" "refused"
fi

test_summary
exit $?
