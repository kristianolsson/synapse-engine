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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from ... import config
from ...core.pipe import IncomingMessage, build_prompt, pipe_to_gemini
from ...core.rate_limiter import RateLimiter
from ...core.session_manager import SessionManager
from ...utils.stats_formatter import format_stats_telegram
from ...utils.html_utils import sanitize_telegram_html
from .task_buttons import parse_tasks, build_task_keyboard, recover_task_from_callback

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

    # Extract parent text so Gemini has context if this is a reply to an empty session
    try:
        reply_to_id = message.reply_to_message.message_id if message.reply_to_message else None
        reply_to_text = message.reply_to_message.text or message.reply_to_message.caption if message.reply_to_message else None
    except AttributeError:
        reply_to_id = None
        reply_to_text = None

    if reply_to_text:
        text = f"Context: You previously sent the user this message: \"{reply_to_text}\"\nThe user replied to that message with: \"{text}\""

    incoming = IncomingMessage(
        source_type="telegram",
        sender=str(user.id),
        subject="",
        body=text,
        image_paths=image_paths,
    )

    parent_session = None
    if reply_to_id:
        parent_session = session_manager.get_message_session(reply_to_id)
        if parent_session:
            session_id = parent_session
            logger.info("Resuming session %s from reply to message_id=%d", session_id, reply_to_id)
        else:
            # Reply to an untracked bot message, start a brand new session
            session_id = None
            logger.info("Starting new session for reply to untracked message_id=%d", reply_to_id)
    else:
        session_id = session_manager.get_session(user_key)

    prompt = build_prompt(incoming)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, pipe_to_gemini, prompt, session_id, None, True)

    if result.is_error and any(s in result.output.lower() for s in ["429", "quota", "rate limit", "capacity", "resource_exhausted", "timeout", "timed out"]):
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Retry (Same Session)", callback_data="quota:retry"),
                InlineKeyboardButton("Fallback (New Session)", callback_data="quota:fallback")
            ]
        ])
        await message.reply_text(f"⚠️ <b>Request Failed</b>\n\n{result.output}", parse_mode='HTML', reply_markup=keyboard)
        return

    if result.session_id and not reply_to_id:
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

    # Check if response contains actionable tasks — attach inline keyboard
    from .task_buttons import format_message_with_tasks, build_task_keyboard
    reply_text, tasks = format_message_with_tasks(reply_text)
    keyboard = build_task_keyboard(tasks)

    # Sanitize HTML for Telegram
    reply_text = sanitize_telegram_html(reply_text)

    sent_message = await message.reply_text(reply_text, parse_mode='HTML', reply_markup=keyboard)
    
    # Save the new message ID tied to this session so the user can keep replying
    if result.session_id and sent_message:
        session_manager.save_message_session(sent_message.message_id, result.session_id)

    # If we just lazily generated a session for an untracked parent, save it to the parent too
    if reply_to_id and not parent_session and result.session_id:
        session_manager.save_message_session(reply_to_id, result.session_id)


async def handle_callback_query(update: Update, context, session_manager: SessionManager) -> None:
    """
    Handle inline keyboard button presses for task completion and undo.

    Recovers the task text from the original message, pipes a completion
    request to Gemini, and updates the pressed button state.
    """
    query = update.callback_query
    if not query or not query.data:
        return
        
    is_done = query.data.startswith("done_")
    is_undo = query.data.startswith("undo_")
    is_quota = query.data.startswith("quota:")
    
    if not is_done and not is_undo and not is_quota:
        return

    task_hash = query.data[5:]  # Strip "done_" or "undo_" (both are 5 chars)
    user = query.from_user

    # Security: check user is allowed
    if user.id not in config.TELEGRAM_ALLOWED_USER_IDS:
        await query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    # --- HANDLE QUOTA BUTTONS ---
    if is_quota:
        action = query.data.split(":")[1]
        await query.answer(f"Executing {action}...")
        
        original_msg = query.message.reply_to_message
        if not original_msg:
            await query.message.edit_text("⚠️ Could not find original message context.")
            return
            
        await query.message.edit_text("⏳ Processing...")
        
        class FakeUpdate:
            message = original_msg
            def get_bot(self): return query.get_bot()
            
        image_paths = await extract_attachments(FakeUpdate())
        text = original_msg.text or original_msg.caption or ""
        
        try:
            reply_to_id = original_msg.reply_to_message.message_id if original_msg.reply_to_message else None
            reply_to_text = original_msg.reply_to_message.text or original_msg.reply_to_message.caption if original_msg.reply_to_message else None
        except AttributeError:
            reply_to_id = None
            reply_to_text = None

        if reply_to_text:
            text = f"Context: You previously sent the user this message: \"{reply_to_text}\"\nThe user replied to that message with: \"{text}\""
            
        incoming = IncomingMessage(
            source_type="telegram",
            sender=str(user.id),
            subject="",
            body=text,
            image_paths=image_paths,
        )
        prompt = build_prompt(incoming)
        
        user_key = str(user.id)
        parent_session = None
        if reply_to_id:
            parent_session = session_manager.get_message_session(reply_to_id)
            session_id = parent_session
        else:
            session_id = session_manager.get_session(user_key)
            
        model = None
        if action == "fallback":
            model = "flash"
            if session_id:
                from ...core.pipe import cleanup_session
                cleanup_session(session_id)
            session_id = None
            
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, pipe_to_gemini, prompt, session_id, model, False)
        
        if result.session_id and not reply_to_id:
            session_manager.save_session(user_key, result.session_id)
            
        reply_text = result.output
        if not reply_text:
            reply_text = "✓"
            
        if session_manager.get_stats_enabled(user_key):
            reply_text += format_stats_telegram(result.stats)
            
        if len(reply_text) > 4096:
            reply_text = reply_text[:4093] + "..."
            
        reply_text, tasks = format_message_with_tasks(reply_text)
        keyboard = build_task_keyboard(tasks)
        
        # Sanitize HTML for Telegram
        reply_text = sanitize_telegram_html(reply_text)

        await query.message.edit_text(reply_text, parse_mode='HTML', reply_markup=keyboard)
        
        if result.session_id:
            session_manager.save_message_session(query.message.message_id, result.session_id)
        if reply_to_id and not parent_session and result.session_id:
            session_manager.save_message_session(reply_to_id, result.session_id)
            
        return

    # Recover task text from the original message
    message_text = query.message.text or ""
    task_text = recover_task_from_callback(message_text, task_hash)

    if not task_text:
        await query.answer("⚠️ Could not identify this task. Please complete it manually.", show_alert=True)
        return

    # Pipe completion request using original message's session if available
    user_key = str(user.id)
    message_id = query.message.message_id if query.message else None
    session_id = None
    
    if message_id:
        session_id = session_manager.get_message_session(message_id)
        
    if not session_id:
        session_id = session_manager.get_session(user_key)

    if is_done:
        prompt = f"Mark the following task as completed: {task_text}"
        await query.answer("Completing task...")
    else:
        prompt = f"Mark the following task as NOT completed (undo): {task_text}"
        await query.answer("Undoing completion...")
    # --- OPTIMISTIC UI UPDATE ---
    old_markup = query.message.reply_markup
    new_markup = None
    updated_text = message_text

    if old_markup:
        updated_buttons = []
        for row in old_markup.inline_keyboard:
            new_row = []
            for btn in row:
                if btn.callback_data == query.data:
                    # Toggle state
                    if is_done:
                        new_text = btn.text.replace("✅", "↩️")
                        new_cb = f"undo_{task_hash}"
                    else:
                        new_text = btn.text.replace("↩️", "✅")
                        new_cb = f"done_{task_hash}"
                    new_row.append(InlineKeyboardButton(new_text, callback_data=new_cb))
                else:
                    new_row.append(btn)
            if new_row:
                updated_buttons.append(new_row)
        
        if updated_buttons:
            new_markup = InlineKeyboardMarkup(updated_buttons)

        # Update the message text: swap ☐ ↔ ✅ for the requested task
        if is_done:
            updated_text = message_text.replace(f"☐ {task_text}", f"✅ {task_text}")
        else:
            updated_text = message_text.replace(f"✅ {task_text}", f"☐ {task_text}")
            
        try:
            await query.message.edit_text(
                updated_text,
                parse_mode='HTML',
                reply_markup=new_markup,
            )
        except Exception as e:
            logger.warning("Could not apply optimistic UI update: %s", e)

    # --- EXECUTE TASK REQUEST IN BACKGROUND ---
    # We fire and forget this so PTB's event loop immediately frees up for the 
    # next callback query (like rapid 'undo' clicks).
    async def _background_task():
        try:
            incoming = IncomingMessage(
                source_type="telegram",
                sender=user_key,
                subject="",
                body=prompt,
            )
            full_prompt = build_prompt(incoming)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, pipe_to_gemini, full_prompt, session_id)

            if result.session_id:
                session_manager.save_session(user_key, result.session_id)

            if result.is_error:
                logger.error("Task completion request failed for '%s': %s", task_text, result.output)
                await query.message.reply_text(f"⚠️ Failed to complete task. Rolling back UI: {task_text}")
                
                # --- ROLLBACK UI UPDATE ---
                if old_markup:
                    try:
                        await query.message.edit_text(
                            message_text,
                            parse_mode='HTML',
                            reply_markup=old_markup,
                        )
                    except Exception as e:
                        logger.warning("Could not rollback message after failed task completion: %s", e)
        except Exception as e:
            logger.error("Error in background task completion: %s", e)

    # Spawn task but keep a reference to avoid garbage collection
    task = asyncio.create_task(_background_task())
    if context is not None:
        if not hasattr(context, "background_tasks"):
            context.background_tasks = set()
        context.background_tasks.add(task)
        task.add_done_callback(context.background_tasks.discard)


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

        async def _callback_handler(update: Update, context) -> None:
            await handle_callback_query(update, context, sm)

        # Handle text messages, photos, documents, and voice notes (for unsupported reply)
        self._app.add_handler(
            MessageHandler(
                filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE,
                _handler,
            )
        )

        # Handle inline keyboard button presses for task completion
        self._app.add_handler(CallbackQueryHandler(_callback_handler))

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
