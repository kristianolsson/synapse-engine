#!/bin/bash
# update.sh — Full rebuild and restart of the synapse Docker container on QNAP.
# Use this when Dockerfile or requirements.txt have changed.
# For code-only updates, use /update in Telegram instead.

set -euo pipefail

SYNAPSE_DIR="/share/CE_CACHEDEV2_DATA/synapse"
COMPOSE_DIR="$SYNAPSE_DIR/synapse-engine"

echo "Pulling latest code..."
docker run --rm --entrypoint sh \
  -v "$SYNAPSE_DIR:$SYNAPSE_DIR" \
  -v "$SYNAPSE_DIR/ssh/id_ed25519:/root/.ssh/id_ed25519" \
  alpine/git \
  -c "chmod 600 /root/.ssh/id_ed25519 && \
      cd $COMPOSE_DIR && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git pull"

echo "Rebuilding image..."
cd "$COMPOSE_DIR"
docker compose build

echo "Restarting container..."
docker compose down
docker compose up -d

echo "Done. Logs:"
docker compose logs --tail=20
