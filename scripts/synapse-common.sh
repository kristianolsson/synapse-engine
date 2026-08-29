#!/bin/bash
# synapse-common.sh — shared helpers for the ./synapse.sh dispatcher.
# Sourced by scripts/synapse-mac.sh, scripts/synapse-qnap.sh, and the tests
# under scripts/tests/.

# Sets the global TARGET to "mac" or "qnap". Must be called directly (not
# inside a `$(...)` subshell) or the assignment is lost.
detect_target() {
    if [[ "$(uname -s)" == "Darwin" ]]; then
        TARGET="mac"
    else
        TARGET="qnap"
    fi
}

# Reads a KEY=value line out of an env file without sourcing it (sourcing
# would choke on comments or execute arbitrary content). Echoes the empty
# string if the file or key is missing. Always takes an explicit file path —
# QNAP has two separate env files (the compose .env and the app-runtime
# .env), so there is no single "the" env file to default to.
# Uses awk to safely handle keys/values with regex metacharacters.
# A single matched pair of surrounding quotes is stripped, matching both
# what `docker compose`'s own dotenv parser does with KEY="value" and what
# the old `set -a; source .env` approach did — without it, a quoted
# SYNAPSE_HOST_DIR would come back with literal quotes in the path.
_env_var() {
    local file="$1" key="$2" value
    value="$(awk -v k="$key" 'BEGIN{FS="="} $1 == k {print substr($0, length($1) + 2); exit}' "$file" 2>/dev/null || true)"
    if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s\n' "$value"
}

# Updates KEY=value in-place if present, else appends it.
# Uses awk to safely handle keys/values with regex/sed metacharacters.
_set_env_var() {
    local file="$1" key="$2" value="$3"

    if [ ! -f "$file" ]; then
        echo "$key=$value" > "$file"
        return
    fi

    # Use awk to update in place or append. On a match the WHOLE line is
    # replaced with "key=value": field-based reassignment ($2 = v with
    # FS/OFS "=") would only replace the first "="-delimited field and
    # re-join the rest, corrupting any value that itself contains "="
    # (TOKEN=abc=def would keep a stray "=def" tail). Rebuilding $0
    # directly also avoids sub()'s special treatment of "&" in the
    # replacement text.
    local temp_file=$(mktemp)
    awk -v k="$key" -v v="$value" '
    BEGIN {
        FS = "="
        found = 0
    }
    $1 == k {
        $0 = k "=" v
        found = 1
    }
    {
        print
    }
    END {
        if (!found) {
            print k "=" v
        }
    }
    ' "$file" > "$temp_file"

    mv "$temp_file" "$file"
}

# Prefers .venv, falls back to venv, empty string if neither has a python.
_detect_venv_dir() {
    local project_dir="$1"
    if [ -f "$project_dir/.venv/bin/python" ]; then
        echo ".venv"
    elif [ -f "$project_dir/venv/bin/python" ]; then
        echo "venv"
    else
        echo ""
    fi
}

# Used by Mac's launchd plist (Gemini CLI needs node on PATH). Empty string
# if node can't be found anywhere reasonable.
_detect_node_bin() {
    if command -v node &>/dev/null; then
        dirname "$(command -v node)"
    elif [ -d "$HOME/.nvm" ]; then
        find "$HOME/.nvm/versions/node" -maxdepth 2 -name bin -type d 2>/dev/null | sort -V | tail -1
    else
        echo ""
    fi
}

SYNAPSE_VAULT_TEMPLATE_URL="https://github.com/kristianolsson/synapse-vault.git"

# Orchestrates the shared clone → detach → personalize → commit → optional
# remote/push flow for a fresh vault. Platform-specific steps are passed in
# as callback function names so QNAP can route every git operation through
# a throwaway container (no git binary on the QNAP host) while Mac runs git
# directly:
#   clone_fn dest_dir        — clones $SYNAPSE_VAULT_TEMPLATE_URL into dest_dir
#   git_fn dir git-args...   — runs `git git-args...` against dir
#   push_fn dir               — pushes dir's current branch to origin
_setup_vault() {
    local vault_dir="$1" clone_fn="$2" git_fn="$3" push_fn="$4"

    if [ -e "$vault_dir" ]; then
        echo "❌ $vault_dir already exists — refusing to overwrite. Remove it or choose a different path."
        return 1
    fi

    echo "Cloning synapse-vault template into $vault_dir..."
    "$clone_fn" "$vault_dir" || { echo "⚠️  Clone failed — vault not set up."; return 1; }

    # Detach: the user's vault becomes its own independent repo from its
    # first real commit, not a fork tracking the public template's history.
    rm -rf "$vault_dir/.git"
    "$git_fn" "$vault_dir" init -q \
        || { echo "⚠️  git init failed — vault left un-versioned at $vault_dir. Continuing."; return 0; }

    if [ -x "$vault_dir/setup.sh" ]; then
        # Fail-soft: a failed personalization still leaves a usable vault,
        # and committing it is better than aborting with a half-set-up one.
        (cd "$vault_dir" && ./setup.sh) \
            || echo "⚠️  The template's setup.sh failed — the vault is still usable but not personalized."
    fi

    "$git_fn" "$vault_dir" add -A
    "$git_fn" "$vault_dir" commit -q -m "Initial commit from synapse-vault template" \
        || { echo "⚠️  Initial commit failed (is git user.name/user.email configured?) — vault left un-versioned at $vault_dir. Continuing."; return 0; }
    echo "✅ Vault ready at $vault_dir (independent git repo)."

    # `|| true`: read returns non-zero at EOF (non-TTY stdin — a piped or
    # `ssh host './synapse.sh setup'` invocation), which under the entrypoint's
    # `set -e` would abort setup mid-way after the vault was already
    # cloned and committed. Empty answer is a valid "skip" here.
    read -rp "Git remote URL for this vault (blank to skip): " vault_remote || true
    if [ -z "$vault_remote" ]; then
        return 0
    fi

    "$git_fn" "$vault_dir" remote add origin "$vault_remote" \
        || { echo "⚠️  Could not add remote 'origin' — continuing without one."; return 0; }
    echo "✅ Remote 'origin' set to $vault_remote"

    read -rp "Push now? [y/N]: " push_answer || true
    if [[ ! "$push_answer" =~ ^[Yy] ]]; then
        echo "Skipped push — the vault has a remote configured but nothing pushed yet."
        return 0
    fi

    "$push_fn" "$vault_dir" \
        || { echo "⚠️  Push failed — the vault is committed locally; push it manually from $vault_dir."; return 0; }
    echo "✅ Pushed."
}
