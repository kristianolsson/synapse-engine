#!/bin/bash
# synapse-mac.sh — Mac/launchd subcommands for the ./synapse dispatcher.
# Ported from the old install.sh/stop.sh, minus vault setup (added in
# scripts/synapse-common.sh + wired in here by a later change).

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_NAME="com.synapse.ingestion.plist"
LABEL="com.synapse.ingestion"

# Writes the launchd plist for this checkout to out_path, substituting the
# template's placeholders. Split out from cmd_setup_mac so it's testable
# without touching launchctl.
_generate_mac_plist() {
    local out_path="$1"
    local node_bin venv_dir
    node_bin="$(_detect_node_bin)"
    if [ -z "$node_bin" ]; then
        node_bin="/usr/local/bin"
    fi
    venv_dir="$(_detect_venv_dir "$PROJECT_DIR")"
    sed \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__NODE_BIN__|$node_bin|g" \
        -e "s|__VENV_DIR__|$venv_dir|g" \
        "$PROJECT_DIR/com.synapse.ingestion.plist.template" > "$out_path"
}

cmd_setup_mac() {
    if [ -z "$(_detect_node_bin)" ]; then
        echo "⚠️  Could not find node. Gemini CLI may not work."
    fi

    if [ -z "$(_detect_venv_dir "$PROJECT_DIR")" ]; then
        echo "❌ venv not found. Create one (.venv or venv) and install requirements."
        exit 1
    fi

    if [ ! -f "$PROJECT_DIR/.env" ]; then
        echo "❌ .env not found. Run: cp .env.example .env"
        exit 1
    fi

    # Runs after the .env/venv guards above so a genuinely fresh clone hits
    # those errors first — writing VAULT_PATH into a not-yet-copied .env
    # would leave setup in a confusing half-configured state.
    if [ -z "$(_env_var "$PROJECT_DIR/.env" VAULT_PATH)" ]; then
        local default_vault_dir
        default_vault_dir="$(cd "$PROJECT_DIR/.." && pwd)/vault"
        echo ""
        echo "No VAULT_PATH configured."
        read -rp "Clone the synapse-vault template to set one up now? [Y/n]: " scaffold_answer
        if [[ ! "$scaffold_answer" =~ ^[Nn] ]]; then
            read -rp "Vault path [$default_vault_dir]: " vault_dir
            vault_dir="${vault_dir:-$default_vault_dir}"
            if _setup_vault "$vault_dir" _mac_vault_clone _mac_vault_git _mac_vault_push; then
                _set_env_var "$PROJECT_DIR/.env" VAULT_PATH "$vault_dir"
                echo "✅ $PROJECT_DIR/.env updated with VAULT_PATH=$vault_dir"
            fi
        else
            echo "Skipping — services will have nowhere to write until VAULT_PATH is set."
        fi
    fi

    _generate_mac_plist "$PROJECT_DIR/$PLIST_NAME"
    echo "✅ Generated $PLIST_NAME"

    echo "Stopping existing service (if running)..."
    launchctl unload "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true
    sleep 2  # let the old process fully exit before reloading

    mkdir -p "$LAUNCH_AGENTS"
    cp "$PROJECT_DIR/$PLIST_NAME" "$LAUNCH_AGENTS/$PLIST_NAME"
    # A prior `./synapse stop --persist` marks the job disabled in launchd's
    # per-user override db, independent of the plist file. Plain `load`
    # fails silently against a disabled job, so clear that first.
    launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl load -w "$LAUNCH_AGENTS/$PLIST_NAME"

    echo "✅ Service installed and started. Logs: ./synapse logs"
}

cmd_start_mac() {
    launchctl load -w "$LAUNCH_AGENTS/$PLIST_NAME"
    echo "Service started."
}

cmd_stop_mac() {
    if [[ "${2:-}" == "--persist" ]]; then
        # unload -w alone errors if the job isn't currently loaded, so
        # load -w first to guarantee it's registered before disabling it.
        launchctl load -w "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true
        launchctl unload -w "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true
        echo "Service stopped and disabled — won't restart on login/reboot. Re-enable with: ./synapse setup"
    else
        if launchctl unload "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null; then
            echo "Service stopped."
        else
            echo "Service was not running."
        fi
        echo "FYI: won't survive reboot/login (RunAtLoad restarts it). Use './synapse stop --persist' to disable permanently."
    fi
}

cmd_restart_mac() {
    cmd_stop_mac
    sleep 2
    cmd_start_mac
}

cmd_update_mac() {
    echo "Pulling latest code..."
    git -C "$PROJECT_DIR" pull
    cmd_restart_mac
}

cmd_logs_mac() {
    tail -f /tmp/synapse-ingestion.out.log /tmp/synapse-ingestion.err.log
}

_mac_vault_clone() {
    git clone "$SYNAPSE_VAULT_TEMPLATE_URL" "$1"
}

_mac_vault_git() {
    local dir="$1"
    shift
    git -C "$dir" "$@"
}

_mac_vault_push() {
    git -C "$1" push -u origin HEAD
}
