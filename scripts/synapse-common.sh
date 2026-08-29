#!/bin/bash
# synapse-common.sh — shared helpers for the ./synapse dispatcher.
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
_env_var() {
    local file="$1" key="$2"
    awk -v k="$key" 'BEGIN{FS="="} $1 == k {print substr($0, length($1) + 2); exit}' "$file" 2>/dev/null || true
}

# Updates KEY=value in-place if present, else appends it.
# Uses awk to safely handle keys/values with regex/sed metacharacters.
_set_env_var() {
    local file="$1" key="$2" value="$3"

    if [ ! -f "$file" ]; then
        echo "$key=$value" > "$file"
        return
    fi

    # Use awk to update in place or append
    local temp_file=$(mktemp)
    awk -v k="$key" -v v="$value" '
    BEGIN {
        FS = "="
        OFS = "="
        found = 0
    }
    $1 == k {
        $2 = v
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
