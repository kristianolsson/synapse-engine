#!/bin/bash
# install.sh — Generate and install the launchd plist for synapse-engine.
# Usage: ./install.sh

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$PROJECT_DIR/com.synapse.ingestion.email.plist.template"
PLIST_NAME="com.synapse.ingestion.email.plist"
PLIST_OUT="$PROJECT_DIR/$PLIST_NAME"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

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
if [ ! -f "$PROJECT_DIR/venv/bin/python" ]; then
    echo "❌ venv not found. Run these first:"
    echo "   python3 -m venv venv"
    echo "   source venv/bin/activate"
    echo "   pip install -r requirements.txt"
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
    "$TEMPLATE" > "$PLIST_OUT"

echo "✅ Generated $PLIST_OUT"
echo "   PROJECT_DIR = $PROJECT_DIR"
echo "   NODE_BIN    = $NODE_BIN"

# --- Install to LaunchAgents ---
# Unload if already loaded
launchctl unload "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true

mkdir -p "$LAUNCH_AGENTS"
cp "$PLIST_OUT" "$LAUNCH_AGENTS/$PLIST_NAME"
launchctl load "$LAUNCH_AGENTS/$PLIST_NAME"

echo "✅ Service installed and started."
echo "   Logs: tail -f /tmp/synapse-ingestion.err.log"
