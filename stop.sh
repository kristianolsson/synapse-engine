#!/bin/bash
# stop.sh — Stop and unload the synapse-engine launchd service.
# Usage: ./stop.sh

set -euo pipefail

PLIST_NAME="com.synapse.ingestion.plist"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

if launchctl unload "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null; then
    echo "Service stopped."
else
    echo "Service was not running."
fi
