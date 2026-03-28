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

# --- AI Provider ---
# Ordered list of providers. First entry is the default; subsequent entries
# are fallback options offered to the user on quota errors.
_raw_providers = os.getenv("AI_PROVIDERS", "").strip()
if not _raw_providers:
    # Backward compat: fall back to the old single-provider env var
    _raw_providers = os.getenv("AI_PROVIDER", "gemini").strip()
AI_PROVIDERS = [p.strip().lower() for p in _raw_providers.split(",") if p.strip()]

_ai_provider = AI_PROVIDERS[0] if AI_PROVIDERS else "gemini"  # mutable runtime value

def get_ai_provider() -> str:
    """Get the current AI provider (may differ from .env after /provider command)."""
    return _ai_provider

def get_ai_providers() -> list[str]:
    """Get the full ordered list of configured providers."""
    return list(AI_PROVIDERS)

def get_next_provider(current: str) -> str | None:
    """Return the next provider in the ordered list after *current*, or None."""
    try:
        idx = AI_PROVIDERS.index(current.strip().lower())
        if idx + 1 < len(AI_PROVIDERS):
            return AI_PROVIDERS[idx + 1]
    except ValueError:
        pass
    return None

def set_ai_provider(provider: str) -> None:
    """Switch the active AI provider at runtime."""
    global _ai_provider
    _ai_provider = provider.strip().lower()

def _resolve_gemini_cmd() -> str:
    """Resolve the gemini CLI path, auto-detecting from the login shell if needed."""
    explicit = os.getenv("GEMINI_CMD", "").strip()
    if explicit:
        return explicit
    # Auto-detect: ask the login shell where gemini lives (handles nvm, etc.)
    import subprocess, shutil
    found = shutil.which("gemini")
    if found:
        return found
    try:
        result = subprocess.run(
            ["bash", "-lc", "which gemini"],
            capture_output=True, text=True, timeout=5,
        )
        path = result.stdout.strip()
        if path and result.returncode == 0:
            return path
    except Exception:
        pass
    return "gemini"

GEMINI_CMD = _resolve_gemini_cmd()
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "120"))
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_FALLBACK_MODELS", "pro,flash,flash-lite").split(",")
    if m.strip()
]

# --- Claude CLI ---
def _resolve_claude_cmd() -> str:
    """Resolve the claude CLI path, auto-detecting from the login shell if needed."""
    explicit = os.getenv("CLAUDE_CMD", "").strip()
    if explicit:
        return explicit
    import subprocess, shutil
    found = shutil.which("claude")
    if found:
        return found
    try:
        result = subprocess.run(
            ["bash", "-lc", "which claude"],
            capture_output=True, text=True, timeout=5,
        )
        path = result.stdout.strip()
        if path and result.returncode == 0:
            return path
    except Exception:
        pass
    return "claude"

CLAUDE_CMD = _resolve_claude_cmd()
CLAUDE_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "300"))
CLAUDE_MAX_RETRIES = int(os.getenv("CLAUDE_MAX_RETRIES", "3"))
CLAUDE_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("CLAUDE_FALLBACK_MODELS", "sonnet,haiku").split(",")
    if m.strip()
]
CLAUDE_MAX_BUDGET_USD = os.getenv("CLAUDE_MAX_BUDGET_USD", "").strip() or None

# --- Telegram Settings ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_IDS = [
    int(uid.strip())
    for uid in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
]
TELEGRAM_MAX_FILE_SIZE_MB = int(os.getenv("TELEGRAM_MAX_FILE_SIZE_MB", "10"))

# --- Channel Selection ---
ENABLED_CHANNELS = [
    ch.strip().lower()
    for ch in os.getenv("ENABLED_CHANNELS", "email").split(",")
    if ch.strip()
]

# --- Stats Display ---
STATS_ENABLED = os.getenv("STATS_ENABLED", "false").strip().lower() in ("true", "1", "yes")

# --- State Maintenance ---
SESSION_STORAGE_PATH = os.getenv(
    "SESSION_STORAGE_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "sessions.json")
)
SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "60"))

# --- Reminder Scheduler ---
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").strip().lower() in ("true", "1", "yes")
SCHEDULER_INTERVAL_MINUTES = int(os.getenv("SCHEDULER_INTERVAL_MINUTES", "60"))

# --- Calendar ---
CALENDAR_CONFIG_PATH = os.getenv(
    "CALENDAR_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "calendars.json")
)
CALENDAR_TOKEN_PATH = os.getenv(
    "CALENDAR_TOKEN_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "token.json")
)
CALENDAR_CREDENTIALS_PATH = os.getenv(
    "CALENDAR_CREDENTIALS_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "credentials.json")
)
