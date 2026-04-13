# QNAP Setup Guide (TS-264)

One-time setup to run synapse on the QNAP. Follow in order.

## Prerequisites

- Container Station installed on the QNAP (provides Docker)
- SSH enabled: QTS → Control Panel → Telnet/SSH → port 22
- A data volume available (this guide uses `CE_CACHEDEV2_DATA` — adjust if yours differs)
- Do NOT use `/share/synapse/` directly — that is a 16MB tmpfs and will fill up immediately

## 1. Create a synapse user on QNAP

In QTS → Control Panel → Users → Create User:
- Username: `synapse`
- No admin privileges, no shared folder access

Then SSH in and confirm the uid:
```bash
id synapse
# Expected: uid=1002(synapse) ...
```

If the uid is different from 1002, update `Dockerfile` line `RUN useradd -m -u 1002 synapse` to match.

## 2. Create folder structure

```bash
mkdir -p /share/CE_CACHEDEV2_DATA/synapse/credentials/claude \
  /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini \
  /share/CE_CACHEDEV2_DATA/synapse/ssh \
  /share/CE_CACHEDEV2_DATA/synapse/notes \
  /share/CE_CACHEDEV2_DATA/synapse/synapse-engine \
  /share/CE_CACHEDEV2_DATA/synapse/data
```

## 3. Generate SSH key for GitHub

```bash
ssh-keygen -t ed25519 -C "synapse@qnap" -f /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519 -N ""
cat /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519.pub
```

Add the public key to GitHub in **two places** (same key, added twice):
- github.com → Settings → SSH and GPG keys → New SSH key
  - Title: `synapse-qnap`, Type: **Authentication Key**
- github.com → Settings → SSH and GPG keys → New SSH key
  - Title: `synapse-qnap-signing`, Type: **Signing Key**

Note: Do NOT use per-repo deploy keys — GitHub won't allow the same key on multiple repos.

## 4. Clone repos

Git is not installed on the QNAP host — use the alpine/git Docker image:

```bash
docker run --rm --entrypoint sh \
  -v /share/CE_CACHEDEV2_DATA/synapse:/share/CE_CACHEDEV2_DATA/synapse \
  -v /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519:/root/.ssh/id_ed25519 \
  alpine/git \
  -c "chmod 600 /root/.ssh/id_ed25519 && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git clone git@github.com:kristianolsson/notes.git /share/CE_CACHEDEV2_DATA/synapse/notes && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git clone git@github.com:kristianolsson/synapse-engine.git /share/CE_CACHEDEV2_DATA/synapse/synapse-engine"
```

## 5. Set up Claude credentials

Build a temporary auth image and log in interactively:

```bash
cat > /tmp/Dockerfile.auth <<'EOF'
FROM node:20-slim
RUN npm install -g @anthropic-ai/claude-code
RUN useradd -m -u 1002 synapse
USER synapse
ENTRYPOINT ["bash"]
EOF

docker build -f /tmp/Dockerfile.auth -t claude-auth-temp /tmp
docker run -it --name claude-login-temp claude-auth-temp
```

Inside the container shell: run `claude`, then `/login`. Complete OAuth via the URL on your Mac browser. Exit the container, then copy the entire `.claude` directory out:

```bash
docker cp claude-login-temp:/home/synapse/.claude/. \
  /share/CE_CACHEDEV2_DATA/synapse/credentials/claude/
docker rm claude-login-temp
docker rmi claude-auth-temp
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/credentials/claude
chmod 755 /share/CE_CACHEDEV2_DATA/synapse/credentials/claude
chmod 644 /share/CE_CACHEDEV2_DATA/synapse/credentials/claude/.credentials.json
```

## 6. Set up Gemini credentials

Copy from Mac (run on Mac):
```bash
scp ~/.gemini/oauth_creds.json ~/.gemini/google_accounts.json ~/.gemini/settings.json \
  admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/gemini/
```

Then on QNAP set ownership and permissions:
```bash
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini
chmod 755 /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini
chmod 644 /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini/*
chmod 664 /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini/oauth_creds.json
```

Note: `oauth_creds.json` needs group-write (664) so the container's synapse user can refresh tokens.

## 7. Set up .env

Copy from Mac:
```bash
scp ~/Documents/code/synapse-engine/.env admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/.env
```

Then on QNAP update the container-specific paths:
```bash
sed -i 's|VAULT_PATH=.*|VAULT_PATH=/app/notes|' /share/CE_CACHEDEV2_DATA/synapse/.env
sed -i 's|CLAUDE_CMD=.*|CLAUDE_CMD=/usr/local/bin/claude|' /share/CE_CACHEDEV2_DATA/synapse/.env
echo "SESSION_STORAGE_PATH=/app/data/sessions.json" >> /share/CE_CACHEDEV2_DATA/synapse/.env
```

## 8. Set permissions

```bash
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/notes
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/data
chmod 755 /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
```

## 9. Build and start

```bash
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
docker compose build
docker compose up -d
docker compose logs -f
```

## Update workflow

When synapse-engine has new commits to deploy:

```bash
# Pull latest code
docker run --rm --entrypoint sh \
  -v /share/CE_CACHEDEV2_DATA/synapse:/share/CE_CACHEDEV2_DATA/synapse \
  -v /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519:/root/.ssh/id_ed25519 \
  alpine/git \
  -c "chmod 600 /root/.ssh/id_ed25519 && cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine && GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' git pull"

# Rebuild and restart
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
docker compose build && docker compose up -d
```

## Token refresh (when auth expires)

**Claude:** Re-run step 5 to log in again and copy fresh credentials directory.

**Gemini:** Re-auth on Mac (`gemini` to trigger browser flow), then re-run the scp in step 6 and `docker compose restart`.
