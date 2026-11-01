"""
Standalone Telegram message sender.

Allows sending messages to a Telegram chat independently of the
listener's inline reply mechanism. Used by the reminder scheduler
to initiate outbound messages.
"""

import html
import logging
import re

import telegram
from telegram.error import BadRequest

from ... import config

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...core.session_manager import UserSession

logger = logging.getLogger(__name__)

# Telegram's hard cap is 4096 characters per message. Chunks are kept a bit
# under it so the closing/re-opening tags added at each split still fit.
TELEGRAM_CHUNK_LIMIT = 4000
# Max length of a single piece before it's packed into a chunk — leaves room
# for the re-opened tags (e.g. a long <a href="...">) at the chunk's start.
_PIECE_LIMIT = TELEGRAM_CHUNK_LIMIT - 500

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>")


def _safe_cut(s: str, limit: int) -> int:
    """Index <= limit to hard-split an over-long line at: prefer a space,
    and never land inside an HTML tag or an &entity;."""
    cut = s.rfind(" ", 0, limit)
    if cut <= limit // 2:
        cut = limit
    lt, gt = s.rfind("<", 0, cut), s.rfind(">", 0, cut)
    if lt > gt and lt > 0:
        cut = lt
    amp, semi = s.rfind("&", 0, cut), s.rfind(";", 0, cut)
    if amp > semi and amp > 0 and cut - amp < 10:
        cut = amp
    return cut


def _pieces(text: str, limit: int):
    """Yield the text line by line (newlines kept), hard-splitting any line
    longer than `limit`."""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            cut = _safe_cut(line, limit)
            yield line[:cut]
            line = line[cut:]
        if line:
            yield line


def _update_stack(stack: list, piece: str) -> list:
    """Return a copy of the open-tag stack after the tags in `piece`."""
    stack = list(stack)
    for m in _TAG_RE.finditer(piece):
        closing, name = m.group(1), m.group(2).lower()
        if name == "br":
            continue
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == name:
                    del stack[i:]
                    break
        else:
            stack.append((name, m.group(0)))
    return stack


def _closers(stack: list) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _openers(stack: list) -> str:
    return "".join(tag for _, tag in stack)


def split_telegram_html(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split Telegram-HTML text into chunks of at most `limit` characters.

    Splits on line boundaries where possible (never inside a tag or entity),
    and closes any tags still open at a split, re-opening them at the start
    of the next chunk, so every chunk is independently valid HTML. A naive
    slice can cut a tag in half, and Telegram then rejects the whole message.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    stack: list = []
    for piece in _pieces(text, min(_PIECE_LIMIT, limit)):
        new_stack = _update_stack(stack, piece)
        if current.strip() and len(current) + len(piece) + len(_closers(new_stack)) > limit:
            chunks.append((current + _closers(stack)).strip())
            current = _openers(stack) + piece
        else:
            current += piece
        stack = new_stack
    if current.strip():
        chunks.append((current + _closers(stack)).strip())
    return [c for c in chunks if c]


def _to_plain_text(text: str) -> str:
    """Strip tags and unescape entities, for the parse-error fallback."""
    return html.unescape(_TAG_RE.sub("", text))


async def send_telegram_message_async(
    chat_id: int, text: str, reply_markup=None, stats: Optional[dict] = None,
    session: Optional["UserSession"] = None,
) -> Optional[int]:
    """
    Send a message to a Telegram chat using the bot API.

    Args:
        chat_id: Telegram chat/user ID to send the message to.
        text: Message text to send.
        reply_markup: Optional InlineKeyboardMarkup for interactive buttons.
        stats: Optional execution statistics to format and append, mirroring
            channels/email/reply.py's send_reply(stats=...).
        session: Optional UserSession handle — if given, gates `stats` on
            its `stats_enabled` (callers can then pass the raw stats dict
            straight through instead of pre-gating it themselves). If
            omitted, `stats` is used as-is (backward compatible with
            callers that already gated it before calling).

    Returns:
        The message_id if sent successfully, None otherwise.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set, cannot send Telegram message")
        return None

    from ...utils.stats_formatter import append_stats_telegram
    text = append_stats_telegram(text, stats, session)

    message_ids = await _send_chunks(chat_id, text, reply_markup)
    return message_ids[-1] if message_ids else None


async def send_telegram_messages_async(
    chat_id: int, text: str, reply_markup=None, stats: Optional[dict] = None,
    session: Optional["UserSession"] = None,
) -> list[int]:
    """Like send_telegram_message_async, but returns every message_id sent
    (a long text is split across several messages), or [] on failure."""
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set, cannot send Telegram message")
        return []

    from ...utils.stats_formatter import append_stats_telegram
    text = append_stats_telegram(text, stats, session)
    return await _send_chunks(chat_id, text, reply_markup)


async def _send_chunks(chat_id: int, text: str, reply_markup=None) -> list[int]:
    """Send `text` split into Telegram-sized chunks. The keyboard goes on the
    last chunk. Each chunk falls back to plain text if Telegram rejects its
    HTML. Returns all message_ids, or [] if any chunk failed to send."""
    chunks = split_telegram_html(text)
    if len(chunks) > 1:
        logger.info("Splitting %d-char Telegram message into %d parts", len(text), len(chunks))
    message_ids: list[int] = []
    try:
        bot = telegram.Bot(token=config.TELEGRAM_BOT_TOKEN)
        for i, chunk in enumerate(chunks):
            markup = reply_markup if i == len(chunks) - 1 else None
            try:
                message = await bot.send_message(chat_id=chat_id, text=chunk, parse_mode='HTML', reply_markup=markup)
            except BadRequest as e:
                err = str(e).lower()
                if "parse entities" not in err and "unexpected end tag" not in err:
                    raise
                logger.warning("Telegram rejected HTML (%s), resending part %d as plain text", e, i + 1)
                message = await bot.send_message(chat_id=chat_id, text=_to_plain_text(chunk), reply_markup=markup)
            message_ids.append(message.message_id)
            logger.info("Telegram message sent to chat_id=%d (msg_id=%d)", chat_id, message.message_id)
        return message_ids
    except Exception as e:
        logger.error(
            "Failed to send Telegram message to chat_id=%d (sent %d of %d parts): %s",
            chat_id, len(message_ids), len(chunks), e,
        )
        return []


def _run_sync(coro):
    """Run a coroutine from sync code, using a worker thread if an event
    loop is already running (safe to call from the scheduler thread)."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop — use a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def send_telegram_message(
    chat_id: int, text: str, reply_markup=None, stats: Optional[dict] = None,
    session: Optional["UserSession"] = None,
) -> Optional[int]:
    """Synchronous wrapper for send_telegram_message_async. Returns the
    last message_id sent (long texts are split), or None on failure."""
    return _run_sync(send_telegram_message_async(chat_id, text, reply_markup, stats, session))


def send_telegram_messages(
    chat_id: int, text: str, reply_markup=None, stats: Optional[dict] = None,
    session: Optional["UserSession"] = None,
) -> list[int]:
    """Synchronous wrapper for send_telegram_messages_async. Returns every
    message_id sent, or [] on failure."""
    return _run_sync(send_telegram_messages_async(chat_id, text, reply_markup, stats, session))
