# Technology Stack

## Core Technology
-   **Language:** Python 3.9+
-   **Environment Management:** `venv`
-   **Configuration:** `.env` file loaded via `python-dotenv`

## Libraries & Frameworks
-   **Email Ingestion:** `imapclient` (IMAP IDLE support)
-   **Telegram Bot:** `python-telegram-bot` (Long-polling)
-   **Testing:** `pytest`, `pytest-cov`

## Infrastructure
-   **Platform:** macOS (local execution)
-   **Process Management:** `launchd` (Daemon mode)
-   **Logging:** Local file system (`/tmp/synapse-ingestion.*.log`)
