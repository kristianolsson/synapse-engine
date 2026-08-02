#!/bin/bash
# install.sh — Generate and install the launchd plist for synapse-engine.
# Usage: ./install.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$PROJECT_DIR/com.synapse.ingestion.plist.template"
PLIST_NAME="com.synapse.ingestion.plist"
PLIST_OUT="$PROJECT_DIR/$PLIST_NAME"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LABEL="com.synapse.ingestion"

# --- Detect node bin directory ---
NODE_BIN=""
if command -v node &>/dev/null; then
    NODE_BIN="$(dirname "$(command -v node)")"
elif [ -d "$HOME/.nvm" ]; then
    # Find the latest nvm-managed node
    NODE_BIN="$(find "$HOME/.nvm/versions/node" -maxdepth 2 -name bin -type d 2>/dev/null | sort -V | tail -1)"
fi

if [ -z "$NODE_BIN" ]; then
    echo "⚠️  Could not find node. Gemini CLI may not work."
    echo "   Install node via nvm or set PATH manually in the plist."
    NODE_BIN="/usr/local/bin"
fi

# --- Validate venv exists ---
VENV_DIR=""
if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then
    VENV_DIR=".venv"
elif [ -f "$PROJECT_DIR/venv/bin/python" ]; then
    VENV_DIR="venv"
else
    echo "❌ venv not found. Please create one (.venv or venv) and install requirements."
    exit 1
fi

# --- Validate .env exists ---
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "❌ .env not found. Run: cp .env.example .env"
    exit 1
fi

# --- Generate plist from template ---
sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__NODE_BIN__|$NODE_BIN|g" \
    -e "s|__VENV_DIR__|$VENV_DIR|g" \
    "$TEMPLATE" > "$PLIST_OUT"

echo "✅ Generated $PLIST_OUT"
echo "   PROJECT_DIR = $PROJECT_DIR"
echo "   NODE_BIN    = $NODE_BIN"

# --- Install to LaunchAgents ---
# Unload if already loaded
echo "Stopping existing service (if running)..."
launchctl unload "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true
sleep 2 # Add a sleep to ensure the old process has fully exited

mkdir -p "$LAUNCH_AGENTS"
cp "$PLIST_OUT" "$LAUNCH_AGENTS/$PLIST_NAME"

# A prior `./stop.sh --persist` marks the job disabled in launchd's
# per-user override db, independent of the plist file. Plain `load`
# fails silently against a disabled job, so clear that first.
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl load -w "$LAUNCH_AGENTS/$PLIST_NAME"

echo "✅ Service installed and started."
echo "   Logs: tail -f /tmp/synapse-ingestion.err.log"

