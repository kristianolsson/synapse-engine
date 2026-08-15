"""
Telegram bot listener with long-polling support.

Receives messages from a private Telegram bot, validates user IDs,
extracts content/attachments, and pipes to the AI provider.
"""

import asyncio
import logging
import os
import re
import signal
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Optional

import pexpect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, ReplyKeyboardRemove
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from ... import config
from ...config import get_next_provider
from ...core import form_state
from ...core.pipe import IncomingMessage, build_prompt, pipe_to_provider
from ...core.rate_limiter import RateLimiter
from ...core.session_manager import SessionManager
from ...utils.stats_formatter import format_stats_telegram
from ...utils.html_utils import sanitize_telegram_html
from ...utils.task_formatter import recover_task_from_callback
from ...utils.form_formatter import render_form_display
from .form_buttons import build_form_keyboard
from .reply_dispatch import build_reply_keyboard, attach_form_message_id

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = config.TELEGRAM_MAX_FILE_SIZE_MB * 1024 * 1024
CLAUDE_AUTH_TIMEOUT_SECONDS = 300

# Pending retry context: maps bot message_id → {prompt, session_id, user_key}
# so quota retry/switch callbacks can re-execute without reply_to_message.
_pending_retries: dict[int, dict] = {}

# Pending Claude re-auth: maps user_key → {child, created_at} between the
# "/update-claude-auth" URL reply and the user pasting back the OAuth code.
_pending_claude_auth: dict[str, dict] = {}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _start_claude_auth() -> tuple[pexpect.spawn, str]:
    """Spawn `claude auth login` and block until it prints the OAuth URL."""
    child = pexpect.spawn(
        "claude", ["auth", "login"], timeout=30, encoding="utf-8", dimensions=(24, 250)
    )
    idx = child.expect([r"https://[^\s\x1b]+", pexpect.EOF, pexpect.TIMEOUT])
    if idx != 0:
        output = _strip_ansi(child.before or "").strip()
        child.close(force=True)
        raise RuntimeError(output or "no output before the process exited")
    return child, child.match.group(0)


def _finish_claude_auth(child: pexpect.spawn, code: str) -> tuple[bool, str]:
    """Send the pasted OAuth code and wait for success/failure."""
    child.sendline(code)
    idx = child.expect(
        ["(?i)success", "(?i)error|invalid|failed", pexpect.EOF, pexpect.TIMEOUT],
        timeout=30,
    )
    output = _strip_ansi((child.before or "") + (child.after if isinstance(child.after, str) else "")).strip()
    child.close(force=True)
    return idx == 0, output


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

    # A reply to a form field's ForceReply prompt is a form answer, not a normal prompt
    if message.reply_to_message and text.strip():
        pending_field = form_state.pop_field_prompt(message.reply_to_message.message_id)
        if pending_field:
            form_id, field_key = pending_field
            if not form_state.get_form(form_id):
                await message.reply_text("That check-in was already submitted.", reply_markup=ReplyKeyboardRemove())
                return
            answer = text.strip()
            form_state.apply_answer(form_id, field_key, answer, answer)
            await _refresh_form_message(context.bot, form_id)
            await message.reply_text(f"Saved: {answer}")
            return

    # Pending Claude re-auth: a plain-text reply while a login is in flight
    # is treated as the pasted OAuth code, not a normal prompt.
    pending_auth = _pending_claude_auth.get(user_key)
    if pending_auth and not text.strip().startswith("/"):
        if time.time() - pending_auth["created_at"] > CLAUDE_AUTH_TIMEOUT_SECONDS:
            pending_auth["child"].close(force=True)
            del _pending_claude_auth[user_key]
            await message.reply_text("Claude login expired. Run /update-claude-auth again.")
            return
        del _pending_claude_auth[user_key]
        await message.reply_text("Completing login...")
        loop = asyncio.get_running_loop()
        try:
            ok, output = await loop.run_in_executor(
                None, _finish_claude_auth, pending_auth["child"], text.strip()
            )
        except Exception as e:
            await message.reply_text(f"Claude login failed: {e}")
            return
        if ok:
            await message.reply_text("✅ Claude login successful.")
        else:
            await message.reply_text(f"❌ Claude login failed:\n{output[-1500:]}")
        return

    if text.strip() in ("/new", "/clear"):
        if session_manager.clear_session(user_key):
            await message.reply_text("Session cleared. Starting a fresh context.")
        else:
            await message.reply_text("No active session to clear.")
        return

    # /help command
    if text.strip() == "/help":
        help_text = (
            "🤖 **Synapse Engine Commands**\n\n"
            "/new, /clear — Clears the current session and starts a fresh context.\n"
            "/stats on|off — Toggles the display of token usage and request stats.\n"
            "/update — Pulls the latest code via git and restarts the bot.\n"
            "/update-cli — Locally updates the Claude and Gemini CLI tools.\n"
            "/update-claude-auth — Re-authenticates the Claude CLI (OAuth login).\n"
            "/provider <gemini|claude|agy> — Switches the active AI provider.\n"
            "/help — Shows this help message."
        )
        await message.reply_text(help_text, parse_mode='Markdown')
        return

    # /stats on|off command
    stripped = text.strip().lower()
    if stripped in ("/stats on", "/stats off"):
        enabled = stripped == "/stats on"
        session_manager.set_stats_enabled(user_key, enabled)
        label = "on" if enabled else "off"
        await message.reply_text(f"Stats display turned {label}.")
        return

    # /update command — git pull + graceful restart (Docker restart: always brings it back)
    if stripped == "/update":
        await message.reply_text("Pulling latest code...")
        try:
            result = subprocess.run(
                ["git", "pull"],
                cwd=Path(__file__).resolve().parents[4],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                output = result.stdout.strip() or "Already up to date."
                await message.reply_text(f"{output}\nRestarting...")
            else:
                await message.reply_text(f"git pull failed:\n{result.stderr.strip()}")
                return
        except Exception as e:
            await message.reply_text(f"Update failed: {e}")
            return
        # Kill our own process rather than PID 1: in Docker this exits tini
        # (PID 1), which the container's `restart: always` policy brings back;
        # locally, launchd's KeepAlive does the same for our own exit.
        os.kill(os.getpid(), signal.SIGTERM)

    # /update-cli command — update Claude and Gemini CLI tools locally
    if stripped == "/update-cli":
        await message.reply_text("Updating CLI tools locally (this might take a minute)...")
        try:
            result = subprocess.run(
                ["npm", "install", "@anthropic-ai/claude-code@latest", "@google/gemini-cli@latest"],
                cwd=Path(__file__).resolve().parents[4],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                await message.reply_text(f"Successfully updated CLI tools:\n{result.stdout.strip()}")
            else:
                await message.reply_text(f"CLI update failed:\n{result.stderr.strip()}")
                return
        except Exception as e:
            await message.reply_text(f"CLI update failed: {e}")
            return
        return

    # /update-claude-auth command — re-run Claude OAuth login in place.
    # ~/.claude is bind-mounted straight to the host credentials dir, so this
    # writes credentials directly without needing SSH or a temp container.
    if stripped == "/update-claude-auth":
        if user_key in _pending_claude_auth:
            _pending_claude_auth[user_key]["child"].close(force=True)
            del _pending_claude_auth[user_key]
        await message.reply_text("Starting Claude login...")
        loop = asyncio.get_running_loop()
        try:
            child, url = await loop.run_in_executor(None, _start_claude_auth)
        except Exception as e:
            await message.reply_text(f"Failed to start Claude login:\n{e}")
            return
        _pending_claude_auth[user_key] = {"child": child, "created_at": time.time()}
        await message.reply_text(
            "Open this URL, sign in, and reply here with the code it gives you "
            f"(expires in {CLAUDE_AUTH_TIMEOUT_SECONDS // 60} min):\n{url}"
        )
        return

    # /amazon command — interact with Amazon Fresh CLI
    if stripped.startswith("/amazon "):
        parts = stripped.split(" ", 1)
        subcmd = parts[1].strip()
        if subcmd == "heal":
            await message.reply_text("⏳ Running Amazon Fresh heal (this takes a minute)...")
            try:
                # Run headlessly in the container
                result = subprocess.run(
                    ["python3", "-m", "services.ingestion.services.amazon_fresh.cli", "heal"],
                    cwd=Path(__file__).resolve().parents[4],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    reply = f"✅ Heal completed:\n```json\n{result.stdout.strip()}\n```"
                    if result.stderr.strip():
                        reply += f"\n\n⚠️ Logs/Warnings:\n```\n{result.stderr.strip()}\n```"
                    await message.reply_text(reply, parse_mode='Markdown')
                else:
                    await message.reply_text(f"❌ Heal failed:\n```\n{result.stdout.strip()}\n{result.stderr.strip()}\n```", parse_mode='Markdown')
            except Exception as e:
                await message.reply_text(f"⚠️ Error running heal: {e}")
        else:
            await message.reply_text(f"Unknown Amazon subcommand: {subcmd}. Available: heal")
        return

    # /provider command
    if stripped.startswith("/provider"):
        parts = stripped.split()
        if len(parts) == 2:
            requested = parts[1]
            if requested in ("gemini", "claude", "agy", "echo"):
                config.set_ai_provider(requested)
                await message.reply_text(f"Switched to {requested} provider.")
            else:
                await message.reply_text(f"Unknown provider: {requested}. Options: gemini, claude, agy")
        else:
            current = config.get_ai_provider()
            await message.reply_text(f"Current provider: {current}. Usage: /provider <gemini|claude|agy>")
        return

    attachment_paths = await extract_attachments(update)

    if message.voice:
        logger.info("Unsupported voice message from user %d", user.id)
        await message.reply_text("Sorry, voice notes are not supported yet.")
        return

    if not text and not attachment_paths:
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
        attachment_paths=attachment_paths,
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
    result = await loop.run_in_executor(None, pipe_to_provider, prompt, session_id, None, True)

    if result.is_error and any(s in result.output.lower() for s in ["429", "quota", "rate limit", "capacity", "resource_exhausted", "timeout", "timed out", "hit your limit", "resets"]):
        provider = result.provider_name or config.get_ai_provider()
        next_provider = get_next_provider(provider)

        buttons = []
        buttons.append(InlineKeyboardButton("🔁 Retry", callback_data="quota:retry"))
        if next_provider:
            buttons.append(InlineKeyboardButton(
                f"🔄 Try with {next_provider}",
                callback_data=f"quota:switch:{next_provider}"
            ))
        keyboard = InlineKeyboardMarkup([buttons])

        friendly = f"⚠️ <b>{provider.capitalize()} hit a limit</b>\n\n{result.output}"
        sent = await message.reply_text(friendly, parse_mode='HTML', reply_markup=keyboard)

        # Stash context so the callback handler can re-execute the prompt
        _pending_retries[sent.message_id] = {
            "prompt": prompt,
            "session_id": session_id,
            "user_key": user_key,
        }
        return

    if result.session_id and not reply_to_id:
        session_manager.save_session(str(user.id), result.session_id)

    reply_text = result.output
    if not reply_text:
        reply_text = "✓"

    # Append stats if enabled for this user
    if session_manager.get_stats_enabled(user_key):
        reply_text += format_stats_telegram(result.stats)

    # Relay error/clarification/response to user.
    # Detect an Actionable Form or task checklist and build the matching keyboard
    # (this also truncates reply_text to Telegram's message limit).
    reply_text, keyboard, form_id = build_reply_keyboard(chat.id, user_key, reply_text)

    # Sanitize HTML for Telegram
    reply_text = sanitize_telegram_html(reply_text)

    try:
        sent_message = await message.reply_text(reply_text, parse_mode='HTML', reply_markup=keyboard)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "unexpected end tag" in str(e).lower():
            logger.warning("Failed to send message with HTML parse_mode due to formatting error, retrying as plain text: %s", e)
            # Remove HTML parse_mode to ensure delivery of malformed output
            sent_message = await message.reply_text(reply_text, reply_markup=keyboard)
        else:
            if form_id:
                form_state.delete_form(form_id)
            raise

    attach_form_message_id(form_id, sent_message.message_id, result.session_id)

    # Save the new message ID tied to this session so the user can keep replying
    if result.session_id and sent_message:
        session_manager.save_message_session(sent_message.message_id, result.session_id)

    # If we just lazily generated a session for an untracked parent, save it to the parent too
    if reply_to_id and not parent_session and result.session_id:
        session_manager.save_message_session(reply_to_id, result.session_id)


# ── Actionable Forms ────────────────────────────────────────────────


async def _refresh_form_message(bot, form_id: str) -> None:
    """Re-render a form's message text and keyboard to reflect current answers."""
    form = form_state.get_form(form_id)
    if not form or not form["message_id"]:
        return
    keyboard = build_form_keyboard(form_id, form["fields"], form["answers"])
    text = render_form_display(form["intro_text"], form["fields"], form["answers"], form["answer_display"])
    text = sanitize_telegram_html(text)
    try:
        await bot.edit_message_text(
            chat_id=form["chat_id"],
            message_id=form["message_id"],
            text=text,
            parse_mode='HTML',
            reply_markup=keyboard,
        )
    except BadRequest as e:
        logger.warning("Failed to refresh form message %s: %s", form_id, e)


async def _handle_form_yn(query, context) -> None:
    """Handle a Yes/No button tap: record the answer, no LLM round-trip."""
    try:
        _, form_id, field_key, value = query.data.split(":", 3)
    except ValueError:
        await query.answer("⚠️ Malformed form action.", show_alert=True)
        return

    if not form_state.get_form(form_id):
        await query.answer("⚠️ Form expired.", show_alert=True)
        return

    await query.answer("Saved.")
    display_value = "Yes" if value == "Y" else "No"
    form_state.apply_answer(form_id, field_key, display_value, value)
    await _refresh_form_message(context.bot, form_id)


async def _handle_form_text_prompt(query, context) -> None:
    """Handle an 'Answer' button tap: prompt for the field via ForceReply."""
    try:
        _, form_id, field_key = query.data.split(":", 2)
    except ValueError:
        await query.answer("⚠️ Malformed form action.", show_alert=True)
        return

    form = form_state.get_form(form_id)
    if not form:
        await query.answer("⚠️ Form expired.", show_alert=True)
        return

    field = next((f for f in form["fields"] if f["key"] == field_key), None)
    if not field:
        await query.answer("⚠️ Unknown field.", show_alert=True)
        return

    await query.answer()
    prompt = await query.message.reply_text(
        f"Reply with: {field['label']}",
        reply_markup=ForceReply(input_field_placeholder=field["label"][:64], selective=True),
    )
    form_state.register_field_prompt(prompt.message_id, form_id, field_key)


async def _handle_form_submit(query, context, session_manager: SessionManager) -> None:
    """Handle the Submit button: batch whatever was answered into one LLM call."""
    try:
        _, form_id = query.data.split(":", 1)
    except ValueError:
        await query.answer("⚠️ Malformed form action.", show_alert=True)
        return

    form = form_state.get_form(form_id)
    if not form:
        await query.answer("⚠️ Form expired.", show_alert=True)
        return

    await query.answer("Submitting...")
    user_key = form["user_key"]
    answers = form["answers"]
    lines = [f"{key}={value}" for key, value in answers.items()]
    prompt_text = "Form submitted:\n" + ("\n".join(lines) if lines else "(no fields answered)")

    incoming = IncomingMessage(
        source_type="telegram",
        sender=user_key,
        subject="",
        body=prompt_text,
    )
    full_prompt = build_prompt(incoming)
    session_id = form.get("session_id") or session_manager.get_session(user_key)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, pipe_to_provider, full_prompt, session_id)

    if result.session_id:
        session_manager.save_session(user_key, result.session_id)

    reply_text = result.output or "✓"
    if session_manager.get_stats_enabled(user_key):
        reply_text += format_stats_telegram(result.stats)
    if len(reply_text) > 4096:
        reply_text = reply_text[:4093] + "..."
    reply_text = sanitize_telegram_html(reply_text)

    rendered = render_form_display(form["intro_text"], form["fields"], form["answers"], form["answer_display"])
    final_text = sanitize_telegram_html(rendered) + "\n\n<i>Submitted.</i>"
    try:
        await query.message.edit_text(final_text, parse_mode='HTML')
    except Exception as e:
        logger.warning("Could not finalize form message %s: %s", form_id, e)

    # Reset the compose box: a ForceReply prompt left unanswered otherwise keeps
    # the client stuck showing its placeholder, since there's no way to retract
    # a ForceReply itself once sent.
    sent = await query.message.reply_text(reply_text, parse_mode='HTML', reply_markup=ReplyKeyboardRemove())
    if result.session_id and sent:
        session_manager.save_message_session(sent.message_id, result.session_id)

    form_state.clear_field_prompts_for_form(form_id)
    form_state.delete_form(form_id)


async def handle_callback_query(update: Update, context, session_manager: SessionManager) -> None:
    """
    Handle inline keyboard button presses for task completion, undo, and forms.

    Recovers the task text from the original message, pipes a completion
    request to Gemini, and updates the pressed button state.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    user = query.from_user

    # Security: check user is allowed
    if user.id not in config.TELEGRAM_ALLOWED_USER_IDS:
        await query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    if query.data.startswith("formyn:"):
        await _handle_form_yn(query, context)
        return
    if query.data.startswith("formtext:"):
        await _handle_form_text_prompt(query, context)
        return
    if query.data.startswith("formsubmit:"):
        await _handle_form_submit(query, context, session_manager)
        return

    is_done = query.data.startswith("done_")
    is_undo = query.data.startswith("undo_")
    is_quota = query.data.startswith("quota:")
    
    if not is_done and not is_undo and not is_quota:
        return

    task_hash = query.data[5:]  # Strip "done_" or "undo_" (both are 5 chars)

    # --- HANDLE QUOTA BUTTONS ---
    if is_quota:
        parts = query.data.split(":")
        action = parts[1]  # 'retry' or 'switch'
        switch_provider = parts[2] if len(parts) > 2 else None
        await query.answer(f"Retrying{'  with ' + switch_provider if switch_provider else ''}...")

        # Look up stashed context from when we showed the quota buttons
        ctx = _pending_retries.pop(query.message.message_id, None)
        if not ctx:
            await query.message.edit_text("⚠️ Retry context expired. Please send your message again.")
            return

        await query.message.edit_text("⏳ Processing...")

        prompt = ctx["prompt"]
        session_id = ctx["session_id"]
        user_key = ctx["user_key"]

        # Determine provider and session handling for the retry
        retry_provider = None
        if action == "switch" and switch_provider:
            retry_provider = switch_provider
            # Start fresh session when switching providers
            if session_id:
                from ...core.pipe import cleanup_session
                cleanup_session(session_id)
            session_id = None
            
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, pipe_to_provider, prompt, session_id, None, False, False, retry_provider
        )
        
        if result.session_id:
            session_manager.save_session(user_key, result.session_id)
            
        reply_text = result.output
        if not reply_text:
            reply_text = "✓"
            
        if session_manager.get_stats_enabled(user_key):
            reply_text += format_stats_telegram(result.stats)
            
        # Detect an Actionable Form or task checklist and build the matching keyboard
        # (this also truncates reply_text to Telegram's message limit).
        reply_text, keyboard, form_id = build_reply_keyboard(query.message.chat.id, user_key, reply_text)

        # Sanitize HTML for Telegram
        reply_text = sanitize_telegram_html(reply_text)

        try:
            await query.message.edit_text(reply_text, parse_mode='HTML', reply_markup=keyboard)
        except Exception as e:
            logger.warning("Could not deliver quota-retry response: %s", e)
            if form_id:
                form_state.delete_form(form_id)
            return

        attach_form_message_id(form_id, query.message.message_id, result.session_id)

        if result.session_id:
            session_manager.save_message_session(query.message.message_id, result.session_id)

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
            result = await loop.run_in_executor(None, pipe_to_provider, full_prompt, session_id)

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
