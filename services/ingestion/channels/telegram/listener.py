"""
Telegram bot listener with long-polling support.

Receives messages from a private Telegram bot, validates user IDs,
extracts content/attachments, and pipes to the Gemini CLI.
"""

import asyncio
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
)

from ... import config
from ...core.pipe import IncomingMessage, build_prompt, pipe_to_gemini
from ...core.rate_limiter import RateLimiter
from ...core.session_manager import SessionManager
from ...utils.stats_formatter import format_stats_telegram

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = config.TELEGRAM_MAX_FILE_SIZE_MB * 1024 * 1024


# ── Attachment Handling ─────────────────────────────────────────────


async def download_attachment(file_obj, filename_hint: str, bot) -> Optional[str]:
    """
    Download a Telegram file to the vault's assets/ingestion/ folder.
    Returns the absolute file path, or None if the file exceeds the size cap.
    """
    if file_obj.file_size and file_obj.file_size > MAX_FILE_BYTES:
        logger.warning(
            "Skipping file %s (%d bytes) — exceeds %dMB cap",
            filename_hint,
            file_obj.file_size,
            config.TELEGRAM_MAX_FILE_SIZE_MB,
        )
        return None

    assets_dir = os.path.join(config.VAULT_PATH, "assets", "ingestion")
    os.makedirs(assets_dir, exist_ok=True)
    today = date.today().isoformat()

    # Sanitize filename
    safe_name = re.sub(r"[^\w.\-]", "_", filename_hint)
    stem, ext = os.path.splitext(safe_name)
    filename = f"{today}_{stem}{ext}"

    filepath = os.path.join(assets_dir, filename)
    counter = 1
    while os.path.exists(filepath):
        filename = f"{today}_{stem}_{counter}{ext}"
        filepath = os.path.join(assets_dir, filename)
        counter += 1

    tg_file = await bot.get_file(file_obj.file_id)
    await tg_file.download_to_drive(filepath)
    logger.info("Downloaded attachment: %s (%d bytes)", filename, file_obj.file_size or 0)
    return filepath


async def extract_attachments(update: Update) -> list[str]:
    """Extract all photos and document attachments from a Telegram message."""
    paths = []
    message = update.message
    bot = update.get_bot()

    # Photos (Telegram sends multiple sizes; take the largest)
    if message.photo:
        photo = message.photo[-1]  # Largest resolution
        path = await download_attachment(photo, f"photo_{photo.file_unique_id}.jpg", bot)
        if path:
            paths.append(path)

    # Documents (files, PDFs, etc.)
    if message.document:
        doc = message.document
        name = doc.file_name or f"file_{doc.file_unique_id}"
        path = await download_attachment(doc, name, bot)
        if path:
            paths.append(path)

    return paths


# ── Message Processing ──────────────────────────────────────────────


async def handle_message(update: Update, context, rate_limiter: RateLimiter, session_manager: SessionManager) -> None:
    """Process an incoming Telegram message."""
    message = update.message
    if not message:
        return

    user = message.from_user
    chat = message.chat

    # Security: private chats only
    if chat.type != "private":
        logger.warning("Ignoring non-private chat (type=%s, id=%d)", chat.type, chat.id)
        return

    # Security: user ID whitelist
    if user.id not in config.TELEGRAM_ALLOWED_USER_IDS:
        logger.warning("Rejected message from unauthorized user: %d", user.id)
        return

    # Rate limiting (shared across channels)
    if not rate_limiter.allow():
        logger.warning("Rate limit reached, ignoring message from user %d", user.id)
        await message.reply_text("⚠️ Rate limit reached. Try again in a minute.", parse_mode='HTML')
        return

    # Extract content
    text = message.text or message.caption or ""
    user_key = str(user.id)

    if text.strip() == "/new":
        if session_manager.clear_session(user_key):
            await message.reply_text("Session cleared. Starting a fresh context.")
        else:
            await message.reply_text("No active session to clear.")
        return

    # /stats on|off command
    stripped = text.strip().lower()
    if stripped in ("/stats on", "/stats off"):
        enabled = stripped == "/stats on"
        session_manager.set_stats_enabled(user_key, enabled)
        label = "on" if enabled else "off"
        await message.reply_text(f"Stats display turned {label}.")
        return

    image_paths = await extract_attachments(update)

    if message.voice:
        logger.info("Unsupported voice message from user %d", user.id)
        await message.reply_text("Sorry, voice notes are not supported yet.")
        return

    if not text and not image_paths:
        logger.info("Empty message from user %d, ignoring", user.id)
        return

    incoming = IncomingMessage(
        source_type="telegram",
        sender=str(user.id),
        subject="",
        body=text,
        image_paths=image_paths,
    )

    prompt = build_prompt(incoming)
    session_id = session_manager.get_session(str(user.id))
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, pipe_to_gemini, prompt, session_id)

    if result.session_id:
        session_manager.save_session(str(user.id), result.session_id)

    reply_text = result.output
    if not reply_text:
        reply_text = "✓"

    # Append stats if enabled for this user
    if session_manager.get_stats_enabled(user_key):
        reply_text += format_stats_telegram(result.stats)

    # Relay error/clarification/response to user
    # Telegram message limit is 4096 chars
    if len(reply_text) > 4096:
        reply_text = reply_text[:4093] + "..."

    await message.reply_text(reply_text, parse_mode='HTML')


# ── Telegram Listener ──────────────────────────────────────────────


class TelegramListener:
    """Telegram bot listener using long-polling."""

    def __init__(self, rate_limiter: Optional[RateLimiter] = None):
        self.rate_limiter = rate_limiter or RateLimiter(
            config.RATE_LIMIT_MAX, config.RATE_LIMIT_WINDOW_SECONDS
        )
        self.session_manager = SessionManager()
        self._app: Optional[Application] = None

    def run(self) -> None:
        """Start the Telegram bot polling loop (blocking)."""
        if not config.TELEGRAM_BOT_TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN not set, cannot start Telegram listener")
            return

        logger.info("Starting Telegram listener...")

        # Silence httpx/httpcore to avoid noisy per-poll log lines
        # and prevent bot token from leaking into logs
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

        # Python 3.9: non-main threads don't have an event loop by default
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        self._app = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .build()
        )

        # Capture state for the closure
        rl = self.rate_limiter
        sm = self.session_manager

        async def _handler(update: Update, context) -> None:
            await handle_message(update, context, rl, sm)

        # Handle text messages, photos, documents, and voice notes (for unsupported reply)
        self._app.add_handler(
            MessageHandler(
                filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE,
                _handler,
            )
        )

        logger.info("Telegram bot polling started.")
        self._app.run_polling(
            drop_pending_updates=True,
            stop_signals=[],  # We handle signals ourselves in main.py
        )

    def stop(self) -> None:
        """Signal the bot to stop."""
        logger.info("Stopping Telegram listener...")
        if self._app and self._app.running:
            # Schedule stop from outside the event loop
            asyncio.get_event_loop().call_soon_threadsafe(
                self._app.stop
            )
