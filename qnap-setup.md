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
  /share/CE_CACHEDEV2_DATA/synapse/credentials/etrade \
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

This one-off bootstrap is only needed before the main `synapse` container exists. Once it's running, use `/update-claude-auth` in Telegram to re-auth (see [Token refresh](#token-refresh)) — no SSH or temp container required, since the running container already bind-mounts `~/.claude` to this same credentials directory.

## 6. Set up Gemini and Antigravity (agy) credentials

Gemini CLI and Antigravity CLI (agy) both store configuration and authentication data under `~/.gemini`.

On Mac:
```bash
# Transfer Gemini OAuth credentials
scp ~/.gemini/oauth_creds.json ~/.gemini/google_accounts.json ~/.gemini/settings.json \
  admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/gemini/

# (Optional) Seed Antigravity CLI configuration and project cache
scp -r ~/.gemini/antigravity-cli \
  admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/gemini/
```

On QNAP:
```bash
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/credentials/gemini
```

## 7. Set up E*TRADE credentials (Optional)

If you use the `etrade` or `options-bot` CLI tools, you must authenticate on your Mac first to bypass E*TRADE's SMS 2FA. E*TRADE recognizes the saved Playwright profile as a "trusted device" and will not prompt the headless Docker container for SMS codes.

On Mac:
```bash
# 1. Run etrade auth locally to generate tokens and trust the browser profile
cd ~/Documents/code/synapse-engine
python3 -m services.ingestion.services.etrade.cli balance

# 2. Transfer the API tokens and browser profile
scp ~/.etrade_tokens admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/etrade/.etrade_tokens
scp -r ~/.etrade_browser_profile admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/etrade/
```

On QNAP:
```bash
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/credentials/etrade
```

## 8. Set up Amazon Fresh credentials (Optional)

If you use the `amazon-fresh` CLI tool, authenticate on your Mac first — Amazon's 2FA and device-trust checks require a headed browser. The saved Firefox profile is then recognized as a "trusted device" in the headless Docker container.

On Mac:
```bash
# 1. Log into Amazon Fresh headed (browser opens automatically)
cd ~/Documents/code/synapse-engine
python3 -m services.ingestion.services.amazon_fresh.cli auth

# 2. Bootstrap selectors from the live pages (also headed)
python3 -m services.ingestion.services.amazon_fresh.cli heal

# 3. Transfer the browser session profile to QNAP
scp -r ~/.amazon-fresh-session admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/amazon/

# 4. Also transfer the updated selectors.json
scp ~/Documents/code/synapse-engine/services/ingestion/services/amazon_fresh/internal/selectors.json \
  admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/synapse-engine/services/ingestion/services/amazon_fresh/internal/
```

On QNAP:
```bash
mkdir -p /share/CE_CACHEDEV2_DATA/synapse/credentials/amazon
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/credentials/amazon

# Note: scp runs as admin, so we must re-chown selectors.json so the container can update it
chown synapse /share/CE_CACHEDEV2_DATA/synapse/synapse-engine/services/ingestion/services/amazon_fresh/internal/selectors.json
```

## 9. Set up .env and config files

On Mac:
```bash
scp ~/Documents/code/synapse-engine/.env admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/.env
scp ~/Documents/code/synapse-engine/calendars.json \
    ~/Documents/code/synapse-engine/credentials.json \
    ~/Documents/code/synapse-engine/token.json \
    admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/synapse-engine/
```

> **Note:** `token.json` covers both Google Calendar and Gmail (single shared OAuth token).
> If you have just added Gmail to an existing setup, delete the old `token.json` first and
> re-run `python -m services.ingestion.shared.google_auth` on your Mac to get a token with
> both scopes before copying it to QNAP.

On QNAP:
```bash
sed -i 's|VAULT_PATH=.*|VAULT_PATH=/app/notes|' /share/CE_CACHEDEV2_DATA/synapse/.env
sed -i 's|CLAUDE_CMD=.*|CLAUDE_CMD=/usr/local/bin/claude|' /share/CE_CACHEDEV2_DATA/synapse/.env
echo "AGY_CMD=/home/synapse/.local/bin/agy" >> /share/CE_CACHEDEV2_DATA/synapse/.env
echo "SESSION_STORAGE_PATH=/app/data/sessions.json" >> /share/CE_CACHEDEV2_DATA/synapse/.env
echo "REMINDERS_JSON_PATH=/app/notes/reminders/reminders.json" >> /share/CE_CACHEDEV2_DATA/synapse/.env
chown -R synapse /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
```

## 9. Build and start

```bash
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
./synapse setup
./synapse logs
```

`./synapse setup` prompts you for which services to enable (writing
`ENABLED_SERVICES` into `/share/CE_CACHEDEV2_DATA/synapse/.env`), then builds
and starts the container. Under the hood it runs:

```bash
docker compose build
docker compose up -d
```

Those raw commands still work if you'd rather set `ENABLED_SERVICES` by hand.

## Update workflow

**Any change** — send `/update` via Telegram (pulls + restarts; a rebuild is
only needed for `Dockerfile`/`requirements.txt` changes, which `/update` itself
doesn't detect — see below), or SSH in and run:
```bash
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
./synapse update
```
`./synapse update` auto-detects whether the pulled commits touched
`Dockerfile`/`requirements.txt` and only rebuilds the image when needed —
otherwise it just restarts, same as `/update` via Telegram.

## Logs

```bash
# Follow live logs (SSH into QNAP first)
cd /share/CE_CACHEDEV2_DATA/synapse/synapse-engine
docker compose logs -f

# Last 100 lines without following
docker compose logs --tail=100
```

## Token refresh

**Claude:** Send `/update-claude-auth` to the bot in Telegram. It replies with an OAuth URL; open it, sign in, and reply with the code it gives you — the bot finishes the login and writes credentials straight to `/share/CE_CACHEDEV2_DATA/synapse/credentials/claude/` (no SSH needed). Falls back to re-running step 5 manually if the bot itself is down or unreachable.

**Gemini:** Re-auth on Mac, re-run scp from step 6, then `docker compose restart`.

**Google (Calendar + Gmail):** Re-auth on Mac, then copy the fresh token to QNAP and restart:
```bash
# On Mac — delete old token to force re-auth
rm ~/Documents/code/synapse-engine/token.json
python -m services.ingestion.shared.google_auth

# Copy fresh token to QNAP
scp ~/Documents/code/synapse-engine/token.json \
    admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/synapse-engine/

# On QNAP
docker compose restart
```
