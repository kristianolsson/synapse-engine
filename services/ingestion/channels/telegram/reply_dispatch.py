"""
Shared "does this reply contain an Actionable Form or a task checklist"
dispatch, used by every place that sends or edits a Telegram message built
from an LLM response: the listener's normal send path, the quota-retry
callback, and the scheduler's reminder delivery path.

This logic previously lived as three separate copies that had already
drifted from each other (see the code-review finding that flagged it) —
centralized here so a fix only has to happen once.
"""

import logging
from typing import Optional

from telegram.error import BadRequest

from ...core import form_state
from ...utils.form_formatter import format_message_with_form
from ...utils.task_formatter import format_message_with_tasks
from .task_buttons import build_task_keyboard
from .form_buttons import build_form_keyboard

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


async def safe_reply_text(message, text: str, parse_mode: Optional[str] = None, **kwargs):
    """
    Send a reply, falling back to plain text if `parse_mode` formatting is
    malformed (unbalanced Markdown entities, stray HTML-like tags, etc).

    Without this, a single unescaped * or < in a reply causes Telegram to
    reject the whole message with a 400, which python-telegram-bot's default
    error handler then swallows — the user sees nothing at all.
    """
    if parse_mode is None:
        return await message.reply_text(text, **kwargs)

    try:
        return await message.reply_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "unexpected end tag" in str(e).lower():
            logger.warning(
                "Failed to send message with parse_mode=%s due to formatting error, retrying as plain text: %s",
                parse_mode, e,
            )
            return await message.reply_text(text, **kwargs)
        raise


async def safe_edit_text(message, text: str, parse_mode: Optional[str] = None, **kwargs):
    """
    Edit a message's text, falling back to plain text if `parse_mode`
    formatting is malformed — the edit_text counterpart to safe_reply_text,
    for callback-query flows (e.g. the quota-retry button) that edit an
    existing message instead of sending a new one.
    """
    if parse_mode is None:
        return await message.edit_text(text, **kwargs)

    try:
        return await message.edit_text(text, parse_mode=parse_mode, **kwargs)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "unexpected end tag" in str(e).lower():
            logger.warning(
                "Failed to edit message with parse_mode=%s due to formatting error, retrying as plain text: %s",
                parse_mode, e,
            )
            return await message.edit_text(text, **kwargs)
        raise


def build_reply_keyboard(chat_id, user_key: str, reply_text: str):
    """
    Truncate to Telegram's message limit, then detect an Actionable Form or
    task checklist and build the matching keyboard.

    Returns (display_text, keyboard, form_id). form_id is None unless an
    Actionable Form was created. Once the message is actually sent/edited,
    call attach_form_message_id() on success or form_state.delete_form() on
    failure — form_id must not be left dangling either way.
    """
    if len(reply_text) > TELEGRAM_MESSAGE_LIMIT:
        reply_text = reply_text[: TELEGRAM_MESSAGE_LIMIT - 3] + "..."

    reply_text, form_fields = format_message_with_form(reply_text)
    if form_fields:
        intro_text = reply_text or "Tap to answer, then hit Submit."
        form_id = form_state.create_form(chat_id, user_key, form_fields, intro_text)
        keyboard = build_form_keyboard(form_id, form_fields, {})
        return intro_text, keyboard, form_id

    reply_text, tasks = format_message_with_tasks(reply_text)
    keyboard = build_task_keyboard(tasks)
    return reply_text, keyboard, None


def attach_form_message_id(form_id: Optional[str], message_id: int, session_id: Optional[str] = None) -> None:
    """Record where a just-sent/edited form message ended up, once delivery succeeded."""
    if not form_id:
        return
    form_state.set_message_id(form_id, message_id)
    if session_id:
        form_state.set_session_id(form_id, session_id)
