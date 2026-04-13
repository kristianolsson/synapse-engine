# QNAP Setup Guide (TS-264)

One-time setup to run synapse on the QNAP. Follow steps in order.

## Prerequisites

- Container Station installed (provides Docker)
- SSH enabled: QTS → Control Panel → Telnet/SSH → port 22
- Use a real data volume — this guide uses `CE_CACHEDEV2_DATA`

## 1. Create synapse user

QTS → Control Panel → Users → Create User:
- Username: `synapse`, no admin privileges

SSH in and note the uid:
```bash
id synapse
# Expected: uid=1002(synapse)
```

If uid differs from 1002, update `Dockerfile`: `RUN useradd -m -u 1002 synapse`

## 2. Create folder structure

```bash
mkdir -p /share/CE_CACHEDEV2_DATA/synapse/credentials/claude \
  /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini \
  /share/CE_CACHEDEV2_DATA/synapse/ssh \
  /share/CE_CACHEDEV2_DATA/synapse/data
```

## 3. Generate SSH key for GitHub

```bash
ssh-keygen -t ed25519 -C "synapse@qnap" \
  -f /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519 -N ""
cat /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519.pub
```

Add the public key to github.com → Settings → SSH and GPG keys, **twice**:
- Title: `synapse-qnap`, Type: **Authentication Key**
- Title: `synapse-qnap-signing`, Type: **Signing Key**

Set ownership:
```bash
chown synapse /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519 \
  /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519.pub
chmod 600 /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519
```

## 4. Clone repos

```bash
docker run --rm --entrypoint sh \
  -v /share/CE_CACHEDEV2_DATA/synapse:/share/CE_CACHEDEV2_DATA/synapse \
  -v /share/CE_CACHEDEV2_DATA/synapse/ssh/id_ed25519:/root/.ssh/id_ed25519 \
  alpine/git \
  -c "chmod 600 /root/.ssh/id_ed25519 && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' \
      git clone git@github.com:kristianolsson/notes.git /share/CE_CACHEDEV2_DATA/synapse/notes && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' \
      git clone git@github.com:kristianolsson/synapse-engine.git /share/CE_CACHEDEV2_DATA/synapse/synapse-engine"
```

Set ownership:
```bash
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/notes
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
```

## 5. Set up Claude credentials

```bash
docker build -f /share/CE_CACHEDEV2_DATA/synapse/synapse-engine/Dockerfile \
  -t claude-auth-temp /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
docker run -it --name claude-login-temp --entrypoint bash claude-auth-temp
```

Inside the container: run `claude`, then `/login`. Complete OAuth on your Mac. Exit, then:

```bash
docker cp claude-login-temp:/home/synapse/.claude/. \
  /share/CE_CACHEDEV2_DATA/synapse/credentials/claude/
docker rm claude-login-temp && docker rmi claude-auth-temp
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/credentials/claude
chmod 755 /share/CE_CACHEDEV2_DATA/synapse/credentials/claude
chmod 644 /share/CE_CACHEDEV2_DATA/synapse/credentials/claude/.credentials.json
```

## 6. Set up Gemini credentials

On Mac:
```bash
scp ~/.gemini/oauth_creds.json ~/.gemini/google_accounts.json ~/.gemini/settings.json \
  admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/gemini/
```

On QNAP:
```bash
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini
chmod 664 /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini/oauth_creds.json
```

## 7. Set up .env

On Mac:
```bash
scp ~/Documents/code/synapse-engine/.env admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/.env
```

On QNAP:
```bash
sed -i 's|VAULT_PATH=.*|VAULT_PATH=/app/notes|' /share/CE_CACHEDEV2_DATA/synapse/.env
sed -i 's|CLAUDE_CMD=.*|CLAUDE_CMD=/usr/local/bin/claude|' /share/CE_CACHEDEV2_DATA/synapse/.env
echo "SESSION_STORAGE_PATH=/app/data/sessions.json" >> /share/CE_CACHEDEV2_DATA/synapse/.env
```

## 8. Build and start

```bash
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
docker compose build
docker compose up -d
docker compose logs -f
```

## Update workflow

**Code changes** (most updates) — send `/update` via Telegram. Synapse pulls the latest code and restarts automatically.

**Dockerfile or requirements.txt changes** — SSH into QNAP and run:
```bash
bash /share/CE_CACHEDEV2_DATA/synapse/synapse-engine/update.sh
```

## Logs

```bash
# Follow live logs (SSH into QNAP first)
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
docker compose logs -f

# Last 100 lines without following
docker compose logs --tail=100
```

## Token refresh

**Claude:** Re-run step 5.

**Gemini:** Re-auth on Mac, re-run scp from step 6, then `docker compose restart`.
