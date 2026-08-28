"""
Standalone Telegram message sender.

Allows sending messages to a Telegram chat independently of the
listener's inline reply mechanism. Used by the reminder scheduler
to initiate outbound messages.
"""

import logging

import telegram

from ... import config

from typing import Optional

logger = logging.getLogger(__name__)


async def send_telegram_message_async(
    chat_id: int, text: str, reply_markup=None, stats: Optional[dict] = None
) -> Optional[int]:
    """
    Send a message to a Telegram chat using the bot API.

    Args:
        chat_id: Telegram chat/user ID to send the message to.
        text: Message text to send.
        reply_markup: Optional InlineKeyboardMarkup for interactive buttons.
        stats: Optional execution statistics to format and append, mirroring
            channels/email/reply.py's send_reply(stats=...) — callers pass
            the raw dict (already gated on the user's /stats preference)
            rather than pre-formatting it themselves.

    Returns:
        The message_id if sent successfully, None otherwise.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set, cannot send Telegram message")
        return None

    if stats:
        from ...utils.stats_formatter import format_stats_telegram
        text += format_stats_telegram(stats)

    try:
        bot = telegram.Bot(token=config.TELEGRAM_BOT_TOKEN)
        # Telegram message limit is 4096 chars
        if len(text) > 4096:
            text = text[:4093] + "..."
        message = await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=reply_markup)
        logger.info("Telegram message sent to chat_id=%d (msg_id=%d)", chat_id, message.message_id)
        return message.message_id
    except Exception as e:
        logger.error("Failed to send Telegram message to chat_id=%d: %s", chat_id, e)
        return None


def send_telegram_message(
    chat_id: int, text: str, reply_markup=None, stats: Optional[dict] = None
) -> Optional[int]:
    """
    Synchronous wrapper for send_telegram_message_async.

    Creates an event loop if needed (safe to call from non-async contexts
    like the scheduler thread).
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop — use a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(
                asyncio.run, send_telegram_message_async(chat_id, text, reply_markup, stats)
            )
            return future.result()
    else:
        return asyncio.run(send_telegram_message_async(chat_id, text, reply_markup, stats))
