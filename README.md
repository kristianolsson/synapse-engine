# Synapse Engine

The "dumb pipe" infrastructure for the **Synapse** system. This service handles external communication (Email, Telegram) and pipes standardized prompts to the Gemini CLI for autonomous management of the **Synapse Vault**.

## Architecture

1.  **Ingestion:** Listens for incoming messages via IMAP IDLE (Email) and/or long-polling (Telegram).
2.  **Standardization:** Wraps content in a YAML metadata block (`Type`, `Sender`, `Context`).
3.  **Pipe:** Invokes the `gemini` CLI within the `notes/` vault.
4.  **Feedback:**
    -   **Email:** Silent on success. Replies only on error/clarification.
    -   **Telegram:** Always replies with a concise confirmation or response.

## Modules

-   `services/ingestion/main.py`: Unified entry point — starts enabled channels in separate threads.
-   `services/ingestion/email_listener.py`: IMAP IDLE loop, sender whitelist, image extraction.
-   `services/ingestion/telegram_listener.py`: Telegram bot long-polling, user ID whitelist, attachment download.
-   `services/ingestion/email_reply.py`: SMTP reply handler with threading support.
-   `services/ingestion/pipe.py`: Formatting and subprocess execution of the Gemini CLI.
-   `services/ingestion/rate_limiter.py`: Shared sliding-window rate limiter across channels.
-   `services/ingestion/config.py`: Environment variable loader.

## Setup

1.  **Prerequisites:**
    -   Python 3.9+
    -   Gemini CLI installed and in PATH.
    -   A dedicated Gmail account for ingestion (with App Password).
    -   (Optional) A Telegram bot token from [@BotFather](https://t.me/BotFather).

2.  **Install:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure:**
    Copy `.env.example` to `.env` and fill in your credentials:
    ```bash
    cp .env.example .env
    nano .env
    ```

    **Key Configuration:**
    - `ENABLED_CHANNELS`: Comma-separated list of channels to run (`email`, `telegram`, or `email,telegram`).
    - `EMAIL_ADDRESS`: The account to ingest from (and reply from).
    - `ALLOWED_SENDERS`: Whitelist of email addresses authorized to send tasks.
    - `REPLY_TO_ADDRESS`: (Optional) Redirect all system replies to this address.
    - `TELEGRAM_BOT_TOKEN`: Bot token from @BotFather.
    - `TELEGRAM_ALLOWED_USER_IDS`: Comma-separated Telegram user IDs authorized to interact with the bot.

    **Telegram Setup:**
    1. Message [@BotFather](https://t.me/BotFather) on Telegram and create a bot (`/newbot`).
    2. Copy the token to `TELEGRAM_BOT_TOKEN` in `.env`.
    3. Get your Telegram user ID (message [@userinfobot](https://t.me/userinfobot)) and add it to `TELEGRAM_ALLOWED_USER_IDS`.
    4. Set `ENABLED_CHANNELS=email,telegram` (or `telegram` for Telegram only).

## Deployment (macOS)

Run as a background service using `launchd`.

1.  **Install & Start:**
    ```bash
    ./install.sh
    ```
    This auto-detects your paths, generates the plist, and installs the service.

2.  **Logs:**
    ```bash
    tail -f /tmp/synapse-ingestion.out.log
    tail -f /tmp/synapse-ingestion.err.log
    ```

## Development

**Run Tests:**
```bash
python -m pytest services/ingestion/tests/ -v
```

**Manual Run:**
```bash
python -m services.ingestion.main
```
