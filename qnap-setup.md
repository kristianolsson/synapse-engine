# QNAP Setup Guide

One-time setup to run synapse on a QNAP NAS (or any Linux host with Docker).
Tested on a QNAP TS-264 with Container Station; adjust as needed for other
models. Follow steps in order.

## Prerequisites

- Container Station installed (provides Docker)
- SSH enabled: QTS → Control Panel → Telnet/SSH → port 22
- A real data volume for persistent storage — this guide uses
  `<YOUR_VOLUME>` (e.g. `CE_CACHEDEV2_DATA`) as a placeholder; substitute
  your NAS's actual volume name throughout

## 0. Choose your host directory and export it

Pick an absolute path on your data volume to hold everything (vault,
engine checkout, credentials). This guide refers to it as `$SYNAPSE_HOST_DIR`.

```bash
export SYNAPSE_HOST_DIR=/share/<YOUR_VOLUME>/synapse
mkdir -p "$SYNAPSE_HOST_DIR"
```

(This `export` only lasts for your current SSH session — re-run it each
time you reconnect, or add it to your shell profile.)

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
mkdir -p "$SYNAPSE_HOST_DIR"/credentials/claude \
  "$SYNAPSE_HOST_DIR"/credentials/gemini \
  "$SYNAPSE_HOST_DIR"/credentials/etrade \
  "$SYNAPSE_HOST_DIR"/ssh \
  "$SYNAPSE_HOST_DIR"/data
```

## 3. Generate SSH key for GitHub

```bash
ssh-keygen -t ed25519 -C "synapse@qnap" \
  -f "$SYNAPSE_HOST_DIR"/ssh/id_ed25519 -N ""
cat "$SYNAPSE_HOST_DIR"/ssh/id_ed25519.pub
```

Add the public key to github.com → Settings → SSH and GPG keys, **twice**:
- Title: `synapse-qnap`, Type: **Authentication Key**
- Title: `synapse-qnap-signing`, Type: **Signing Key**

Set ownership:
```bash
chown synapse "$SYNAPSE_HOST_DIR"/ssh/id_ed25519 \
  "$SYNAPSE_HOST_DIR"/ssh/id_ed25519.pub
chmod 600 "$SYNAPSE_HOST_DIR"/ssh/id_ed25519
```

## 4. Clone repos

Replace `YOUR_GITHUB_USERNAME` below with the account that owns your vault
repo (see [synapse-vault](https://github.com/kristianolsson/synapse-vault)
for a starting template) and `synapse-engine`. The vault repo can be named
anything on GitHub — it's cloned into a local folder named `vault` here
because that's the folder name `docker-compose.yml`'s mount expects.

```bash
docker run --rm --entrypoint sh \
  -v "$SYNAPSE_HOST_DIR:$SYNAPSE_HOST_DIR" \
  -v "$SYNAPSE_HOST_DIR/ssh/id_ed25519:/root/.ssh/id_ed25519" \
  alpine/git \
  -c "chmod 600 /root/.ssh/id_ed25519 && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' \
      git clone git@github.com:YOUR_GITHUB_USERNAME/vault.git $SYNAPSE_HOST_DIR/vault && \
      GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=no' \
      git clone git@github.com:YOUR_GITHUB_USERNAME/synapse-engine.git $SYNAPSE_HOST_DIR/synapse-engine"
```

Set ownership:
```bash
chown -R synapse "$SYNAPSE_HOST_DIR"/vault
chown -R synapse "$SYNAPSE_HOST_DIR"/synapse-engine
```

Create the compose-local `.env` (drives `docker-compose.yml` variable
substitution — separate from the runtime `.env` in step 9):
```bash
cp "$SYNAPSE_HOST_DIR"/synapse-engine/.env.compose.example "$SYNAPSE_HOST_DIR"/synapse-engine/.env
sed -i "s|SYNAPSE_HOST_DIR=.*|SYNAPSE_HOST_DIR=$SYNAPSE_HOST_DIR|" "$SYNAPSE_HOST_DIR"/synapse-engine/.env
# Optionally set GIT_USER_NAME / GIT_USER_EMAIL in that same file if you
# want commits to your vault authored as you instead of the default bot identity.
```

## 5. Set up Claude credentials

```bash
docker build -f "$SYNAPSE_HOST_DIR"/synapse-engine/Dockerfile \
  -t claude-auth-temp "$SYNAPSE_HOST_DIR"/synapse-engine
docker run -it --name claude-login-temp --entrypoint bash claude-auth-temp
```

Inside the container: run `claude`, then `/login`. Complete OAuth on your Mac. Exit, then:

```bash
docker cp claude-login-temp:/home/synapse/.claude/. \
  "$SYNAPSE_HOST_DIR"/credentials/claude/
docker rm claude-login-temp && docker rmi claude-auth-temp
chown -R synapse "$SYNAPSE_HOST_DIR"/credentials/claude
chmod 755 "$SYNAPSE_HOST_DIR"/credentials/claude
chmod 644 "$SYNAPSE_HOST_DIR"/credentials/claude/.credentials.json
```

This one-off bootstrap is only needed before the main `synapse` container exists. Once it's running, use `/update-claude-auth` in Telegram to re-auth (see [Token refresh](#token-refresh)) — no SSH or temp container required, since the running container already bind-mounts `~/.claude` to this same credentials directory.

## 6. Set up Gemini and Antigravity (agy) credentials

Gemini CLI and Antigravity CLI (agy) both store configuration and authentication data under `~/.gemini`.

On Mac:
```bash
# Transfer Gemini OAuth credentials
scp ~/.gemini/oauth_creds.json ~/.gemini/google_accounts.json ~/.gemini/settings.json \
  admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/credentials/gemini/

# (Optional) Seed Antigravity CLI configuration and project cache
scp -r ~/.gemini/antigravity-cli \
  admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/credentials/gemini/
```

On QNAP:
```bash
chown -R synapse "$SYNAPSE_HOST_DIR"/credentials/gemini
```

## 7. Set up E*TRADE credentials (Optional)

If you use the `etrade` or `options-bot` CLI tools, you must authenticate on your Mac first to bypass E*TRADE's SMS 2FA. E*TRADE recognizes the saved Playwright profile as a "trusted device" and will not prompt the headless Docker container for SMS codes.

On Mac:
```bash
# 1. Run etrade auth locally to generate tokens and trust the browser profile
cd ~/Documents/code/synapse-engine
python3 -m services.ingestion.tools.etrade_cli balance

# 2. Transfer the API tokens and browser profile
scp ~/.etrade_tokens admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/credentials/etrade/.etrade_tokens
scp -r ~/.etrade_browser_profile admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/credentials/etrade/
```

On QNAP:
```bash
chown -R synapse "$SYNAPSE_HOST_DIR"/credentials/etrade
```

## 8. Set up Amazon Fresh credentials (Optional)

If you use the `amazon-fresh` CLI tool, authenticate on your Mac first — Amazon's 2FA and device-trust checks require a headed browser. The saved Firefox profile is then recognized as a "trusted device" in the headless Docker container.

On Mac:
```bash
# 1. Log into Amazon Fresh headed (browser opens automatically)
cd ~/Documents/code/synapse-engine
python3 -m services.ingestion.tools.amazon_fresh_cli auth

# 2. Bootstrap selectors from the live pages (also headed)
python3 -m services.ingestion.tools.amazon_fresh_cli heal

# 3. Transfer the browser session profile to QNAP
scp -r ~/.amazon-fresh-session admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/credentials/amazon/

# 4. Also transfer the updated selectors.json
scp ~/Documents/code/synapse-engine/services/ingestion/tools/amazon_fresh/selectors.json \
  admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/synapse-engine/services/ingestion/tools/amazon_fresh/
```

On QNAP:
```bash
mkdir -p "$SYNAPSE_HOST_DIR"/credentials/amazon
chown -R synapse "$SYNAPSE_HOST_DIR"/credentials/amazon

# Note: scp runs as admin, so we must re-chown selectors.json so the container can update it
chown synapse "$SYNAPSE_HOST_DIR"/synapse-engine/services/ingestion/tools/amazon_fresh/selectors.json
```

## 9. Set up .env and config files

On Mac:
```bash
scp ~/Documents/code/synapse-engine/.env admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/.env
scp ~/Documents/code/synapse-engine/calendars.json \
    ~/Documents/code/synapse-engine/credentials.json \
    ~/Documents/code/synapse-engine/token.json \
    admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/synapse-engine/
```

> **Note:** `token.json` covers both Google Calendar and Gmail (single shared OAuth token).
> If you have just added Gmail to an existing setup, delete the old `token.json` first and
> re-run `python -m services.ingestion.tools.setup_google` on your Mac to get a token with
> both scopes before copying it to QNAP.

> **Note:** this is the *runtime* `.env` (mounted into the container at
> boot), separate from the compose-local `.env` created in step 4.

On QNAP:
```bash
sed -i 's|VAULT_PATH=.*|VAULT_PATH=/app/notes|' "$SYNAPSE_HOST_DIR"/.env
sed -i 's|CLAUDE_CMD=.*|CLAUDE_CMD=/usr/local/bin/claude|' "$SYNAPSE_HOST_DIR"/.env
echo "AGY_CMD=/home/synapse/.local/bin/agy" >> "$SYNAPSE_HOST_DIR"/.env
echo "SESSION_STORAGE_PATH=/app/data/sessions.json" >> "$SYNAPSE_HOST_DIR"/.env
echo "REMINDERS_JSON_PATH=/app/notes/reminders/reminders.json" >> "$SYNAPSE_HOST_DIR"/.env
chown -R synapse "$SYNAPSE_HOST_DIR"/synapse-engine
```

## 10. Build and start

```bash
cd "$SYNAPSE_HOST_DIR"/synapse-engine
docker compose build
docker compose up -d
docker compose logs -f
```

## Update workflow

**Code changes** (most updates) — send `/update` via Telegram. Synapse pulls the latest code and restarts automatically.

**Dockerfile or requirements.txt changes** — SSH into QNAP and run:
```bash
bash "$SYNAPSE_HOST_DIR"/synapse-engine/update.sh
```

## Logs

```bash
# Follow live logs (SSH into QNAP first)
cd "$SYNAPSE_HOST_DIR"/synapse-engine
docker compose logs -f

# Last 100 lines without following
docker compose logs --tail=100
```

## Token refresh

**Claude:** Send `/update-claude-auth` to the bot in Telegram. It replies with an OAuth URL; open it, sign in, and reply with the code it gives you — the bot finishes the login and writes credentials straight to `$SYNAPSE_HOST_DIR/credentials/claude/` (no SSH needed). Falls back to re-running step 5 manually if the bot itself is down or unreachable.

**Gemini:** Re-auth on Mac, re-run scp from step 6, then `docker compose restart`.

**Google (Calendar + Gmail):** Re-auth on Mac, then copy the fresh token to QNAP and restart:
```bash
# On Mac — delete old token to force re-auth
rm ~/Documents/code/synapse-engine/token.json
python -m services.ingestion.tools.setup_google

# Copy fresh token to QNAP
scp ~/Documents/code/synapse-engine/token.json \
    admin@<QNAP_IP>:$SYNAPSE_HOST_DIR/synapse-engine/

# On QNAP
docker compose restart
```
