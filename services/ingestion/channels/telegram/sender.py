"""
Standalone Telegram message sender.

Allows sending messages to a Telegram chat independently of the
listener's inline reply mechanism. Used by the reminder scheduler
to initiate outbound messages.
"""

import logging

import telegram

from ... import config

logger = logging.getLogger(__name__)


async def send_telegram_message_async(chat_id: int, text: str) -> bool:
    """
    Send a message to a Telegram chat using the bot API.

    Args:
        chat_id: Telegram chat/user ID to send the message to.
        text: Message text to send.

    Returns:
        True if sent successfully, False otherwise.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set, cannot send Telegram message")
        return False

    try:
        bot = telegram.Bot(token=config.TELEGRAM_BOT_TOKEN)
        # Telegram message limit is 4096 chars
        if len(text) > 4096:
            text = text[:4093] + "..."
        await bot.send_message(chat_id=chat_id, text=text)
        logger.info("Telegram message sent to chat_id=%d", chat_id)
        return True
    except Exception as e:
        logger.error("Failed to send Telegram message to chat_id=%d: %s", chat_id, e)
        return False


def send_telegram_message(chat_id: int, text: str) -> bool:
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
            future = pool.submit(asyncio.run, send_telegram_message_async(chat_id, text))
            return future.result()
    else:
        return asyncio.run(send_telegram_message_async(chat_id, text))
