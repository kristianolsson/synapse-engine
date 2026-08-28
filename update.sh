#!/bin/bash
# update.sh — Full rebuild and restart of the synapse Docker container on QNAP.
# Use this when Dockerfile or requirements.txt have changed.
# For code-only updates, use /update in Telegram instead.

set -euo pipefail

cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi
SYNAPSE_DIR="${SYNAPSE_HOST_DIR:?Set SYNAPSE_HOST_DIR in synapse-engine/.env — see .env.compose.example}"
COMPOSE_DIR="$SYNAPSE_DIR/synapse-engine"

echo "Pulling latest code..."
docker run --rm --entrypoint sh \
  -v "$SYNAPSE_DIR:$SYNAPSE_DIR" \
  -v "$SYNAPSE_DIR/ssh/id_ed25519:/root/.ssh/id_ed25519" \
  alpine/git \
  -c "chmod 600 /root/.ssh/id_ed25519 && \
      git config --global --add safe.directory $COMPOSE_DIR && \
      cd $COMPOSE_DIR && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git pull"

chown -R synapse "$COMPOSE_DIR"

echo "Rebuilding image..."
cd "$COMPOSE_DIR"
docker compose build

echo "Restarting container..."
docker compose down
docker compose up -d

echo "Done. Logs:"
docker compose logs --tail=20
