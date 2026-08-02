#!/bin/bash
# stop.sh — Stop and unload the synapse-engine launchd service.
# Usage: ./stop.sh [--persist]
#   --persist   Also disable the service so it does not come back on login/reboot.

set -euo pipefail

PLIST_NAME="com.synapse.ingestion.plist"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LABEL="com.synapse.ingestion"

if [[ "${1:-}" == "--persist" ]]; then
    # unload -w alone errors out if the job isn't currently loaded, so
    # load -w first to guarantee it's registered before disabling it.
    launchctl load -w "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true
    launchctl unload -w "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null || true
    echo "Service stopped and disabled — it will not restart on login/reboot."
    echo "(Re-enable with: ./install.sh)"
else
    if launchctl unload "$LAUNCH_AGENTS/$PLIST_NAME" 2>/dev/null; then
        echo "Service stopped."
    else
        echo "Service was not running."
    fi
    echo "FYI: this stop won't survive a reboot/login (RunAtLoad will restart it). Use './stop.sh --persist' to disable it permanently."
fi
