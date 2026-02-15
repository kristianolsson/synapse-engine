# Life-OS Engine

The "dumb pipe" infrastructure for the Gemini Life-OS. This service handles external communication (Email, Telegram) and pipes standardized prompts to the Gemini CLI for autonomous vault management.

## Architecture

1.  **Ingestion:** Listens for incoming messages via IMAP IDLE (Email) or Webhook (Telegram).
2.  **Standardization:** Wraps content in a YAML metadata block (`Type`, `Sender`, `Context`).
3.  **Pipe:** Invokes the `gemini` CLI within the `notes/` vault.
4.  **Feedback:**
    -   **Success:** CLI outputs nothing (silent).
    -   **Error/Clarification:** CLI outputs text -> Service replies to the sender.

## Modules

-   `services/ingestion/email_listener.py`: IMAP IDLE loop, sender whitelist, rate limiting, image extraction.
-   `services/ingestion/email_reply.py`: SMTP reply handler with threading support.
-   `services/ingestion/pipe.py`: Formatting and subprocess execution of the Gemini CLI.
-   `config.py`: Environment variable loader.

## Setup

1.  **Prerequisites:**
    -   Python 3.9+
    -   Gemini CLI installed and in PATH.
    -   A dedicated Gmail account for ingestion (with App Password).

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

## Deployment (macOS)

Run as a background service using `launchd`.

1.  **Install Plist:**
    ```bash
    cp com.lifeos.ingestion.email.plist ~/Library/LaunchAgents/
    ```

2.  **Load Service:**
    ```bash
    launchctl load ~/Library/LaunchAgents/com.lifeos.ingestion.email.plist
    ```

3.  **Logs:**
    ```bash
    tail -f /tmp/lifeos-ingestion.out.log
    tail -f /tmp/lifeos-ingestion.err.log
    ```

## Development

**Run Tests:**
```bash
python -m pytest services/ingestion/tests/
```

**Manual Run:**
```bash
python -m services.ingestion.email_listener
```
