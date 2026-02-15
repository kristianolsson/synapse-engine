"""
Configuration loader for the ingestion service.
Reads credentials and settings from environment variables / .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the repo root (two levels up from this file)
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_ENV_PATH)


# --- IMAP Settings ---
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))

# --- SMTP Settings ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# --- Credentials ---
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")

# --- Whitelist ---
ALLOWED_SENDERS = [
    s.strip().lower()
    for s in os.getenv("ALLOWED_SENDERS", "").split(",")
    if s.strip()
]

# --- Reply Settings ---
# If set, all system replies go to this address instead of the original sender.
REPLY_TO_ADDRESS = os.getenv("REPLY_TO_ADDRESS", "").strip().lower()

# --- Vault ---
VAULT_PATH = os.getenv("VAULT_PATH", str(Path(__file__).resolve().parent.parent.parent / "notes"))

# --- Rate Limiting ---
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "10"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

# --- Gemini CLI ---
GEMINI_CMD = os.getenv("GEMINI_CMD", "gemini")
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "120"))
