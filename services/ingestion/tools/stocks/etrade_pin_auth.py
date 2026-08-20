"""Manual OAuth PIN fallback for E*TRADE authentication.

Automated Playwright-driven login gets blocked by E*TRADE's bot detection
(Akamai Bot Manager + RSA/Cyota device fingerprinting reject the automated
form submission regardless of typing speed or mouse movement — confirmed
by direct testing). The only reliable path is a human completing the
OAuth authorize flow themselves in a real, unautomated browser.

This implements the classic three-legged OAuth PIN flow (the same one
services/ingestion/tools/stocks/auth.py's ETradeAuth uses interactively),
split into two steps so the verification code can round-trip through a
messaging channel (Telegram/email) instead of a blocking terminal input(),
and so it survives across process boundaries: a scheduled job's
short-lived subprocess sends the prompt (start_pin_auth), and the
long-lived Telegram/email listener process completes the exchange
(finish_pin_auth) whenever the reply arrives.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from authlib.integrations.requests_client import OAuth1Session

logger = logging.getLogger(__name__)

PENDING_FILE = Path.home() / ".etrade_pending_auth.json"
PENDING_TTL_SECONDS = 30 * 60
TOKEN_FILE = Path.home() / ".etrade_tokens"


def load_pending() -> Optional[dict]:
    """Return the pending auth request, or None if there isn't one or it expired."""
    if not PENDING_FILE.exists():
        return None
    try:
        data = json.loads(PENDING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("created_at", 0) > PENDING_TTL_SECONDS:
        clear_pending()
        return None
    return data


def clear_pending() -> None:
    PENDING_FILE.unlink(missing_ok=True)


def mark_prompt_sent(**fields) -> None:
    """Generic merge helper: layers arbitrary fields (channel, chat_id,
    prompt_message_id, retry correlation, etc.) onto the pending request,
    usable at any point in the flow — not strictly after the prompt has
    been delivered (e.g. _fallback_to_pin_auth calls it to record retry
    correlation before the prompt is actually sent). No-op if there's no
    pending request (e.g. it expired between start_pin_auth and the call)."""
    pending = load_pending()
    if pending is None:
        return
    pending.update(fields)
    PENDING_FILE.write_text(json.dumps(pending))


def backfill_session_id(session_key: str, session_id: str) -> None:
    """If a pending E*TRADE auth request is waiting on this exact
    session_key but didn't yet know its session_id (it was the first
    message in a brand-new thread when the auth fallback fired), fill
    it in now that the provider has assigned one. No-op otherwise."""
    if not session_id:
        return
    pending = load_pending()
    if pending and pending.get("session_key") == session_key and not pending.get("session_id"):
        mark_prompt_sent(session_id=session_id)


def start_pin_auth(consumer_key: str, consumer_secret: str) -> dict:
    """Fetch a request token + authorize URL from E*TRADE and persist
    pending state for finish_pin_auth to complete later, possibly from a
    different process. Call mark_prompt_sent() once the prompt has
    actually been delivered, to record which channel/message to match
    the reply against.

    Returns the pending dict, including 'authorize_url' to send to the user.
    """
    client = OAuth1Session(client_id=consumer_key, client_secret=consumer_secret, redirect_uri="oob")
    request_token = client.fetch_request_token(
        url="https://api.etrade.com/oauth/request_token", params={"format": "json"}
    )
    authorize_url = (
        f"https://us.etrade.com/e/t/etws/authorize?key={consumer_key}&token={request_token['oauth_token']}"
    )
    pending = {
        "oauth_token": request_token["oauth_token"],
        "oauth_token_secret": request_token["oauth_token_secret"],
        "authorize_url": authorize_url,
        "created_at": time.time(),
    }
    PENDING_FILE.write_text(json.dumps(pending))
    PENDING_FILE.chmod(0o600)
    return pending


def finish_pin_auth(pending: dict, verifier: str, consumer_key: str, consumer_secret: str) -> dict:
    """Exchange the pasted verification code for a real access token.

    Does not clear the pending file — the caller clears it only after
    also saving the resulting token, so a crash in between doesn't lose
    the in-flight request-token state.
    """
    client = OAuth1Session(
        client_id=consumer_key,
        client_secret=consumer_secret,
        token=pending["oauth_token"],
        token_secret=pending["oauth_token_secret"],
        redirect_uri="oob",
    )
    client.fetch_access_token(url="https://api.etrade.com/oauth/access_token", verifier=verifier.strip())
    return {
        "access_token": client.token.get("oauth_token"),
        "access_token_secret": client.token.get("oauth_token_secret"),
    }


def save_access_token(access_token: str, access_token_secret: str, sandbox: bool = False) -> None:
    """Save the exchanged token to the same file/schema
    ETradeAuth/WetradeAuth read from on their next run, so the normal
    login flow picks it up transparently."""
    data = {"access_token": access_token, "access_token_secret": access_token_secret, "sandbox": sandbox}
    TOKEN_FILE.write_text(json.dumps(data))
    TOKEN_FILE.chmod(0o600)


def complete_and_maybe_retry(pending: dict) -> None:
    """After a successful PIN completion, resume the exact session that
    failed (interactive case) or replay the reminder task fresh
    (scheduled case) — whichever fields `_fallback_to_pin_auth` recorded
    on `pending`. No-op if neither is present (e.g. a manual
    /update-etrade-auth run has nothing to retry)."""
    session_key = pending.get("session_key")
    reminder_task = pending.get("reminder_task")

    if session_key:
        logger.info("complete_and_maybe_retry: resuming session %s (session-resume branch)", session_key)
        from ...core.pipe import pipe_to_provider, build_prompt, IncomingMessage
        from ...core.session_manager import SessionManager

        failed_command = pending.get("failed_command", "")
        retry_channel = pending.get("retry_channel", "system")
        # Route through the same envelope every real message gets (Type/
        # Sender/Context/Current Time) instead of a bare string — an
        # unattributed instruction arriving mid-session reads as a prompt
        # injection attempt, and got correctly flagged as one in practice.
        sender = pending.get("email_to") if retry_channel == "email" else session_key
        retry_text = (
            "E*TRADE authentication just completed successfully. You were "
            f"blocked earlier in this conversation when running `{failed_command}` "
            "— retry that now and complete my full original request."
        )
        retry_prompt = build_prompt(IncomingMessage(source_type=retry_channel, sender=sender or "system", body=retry_text))
        result = pipe_to_provider(retry_prompt, session_id=pending.get("session_id") or None)
        if result.session_id:
            SessionManager().save_session(session_key, result.session_id)
        _deliver_retry_result(pending, result.output or "✓")

    elif reminder_task:
        logger.info("complete_and_maybe_retry: replaying reminder task %r (reminder-replay branch)", reminder_task)
        from ...core.pipe import pipe_to_provider, build_prompt, IncomingMessage

        # Deliberately simplified compared to scheduler.py's
        # _handle_work_reminder: no quota-fallback provider switch, no
        # session-save-for-reply-continuation, no form-keyboard rendering.
        # Acceptable for a degraded auth-recovery path — this isn't meant
        # to fully replicate the scheduler's normal delivery, just get the
        # result back to the user once auth is unblocked.
        incoming = IncomingMessage(
            source_type="scheduled_work",
            sender="system",
            subject="Scheduled Work Task",
            body=reminder_task,
        )
        prompt = build_prompt(incoming)
        result = pipe_to_provider(prompt, model="work")
        text = result.output or f"✓ Scheduled task completed: {reminder_task}"
        _deliver_retry_result(pending, text)


def _deliver_retry_result(pending: dict, text: str) -> None:
    """Send the retry/replay result to wherever the original request
    came from (not necessarily wherever the PIN reply was completed)."""
    retry_channel = pending.get("retry_channel")
    if retry_channel == "telegram":
        from ...channels.telegram.sender import send_telegram_message
        from ...utils.html_utils import sanitize_telegram_html

        # Telegram-delivered retries prefer the dedicated retry_chat_id
        # (the chat of whoever's original REQUEST is being retried) but
        # fall back to the plain chat_id field, which is always populated
        # for the scheduled-reminder replay case (no SYNAPSE_CHAT_ID/
        # retry_chat_id is ever set for reminders).
        chat_id = pending.get("retry_chat_id") or pending.get("chat_id")
        if chat_id:
            send_telegram_message(chat_id, sanitize_telegram_html(text))
        else:
            logger.warning("Retry delivery skipped: retry_channel=telegram but no chat_id/retry_chat_id on pending record")
    elif retry_channel == "email":
        from ... import config
        from ...channels.email.reply import send_reply

        to_addr = pending.get("email_to") or config.REPLY_TO_ADDRESS
        if to_addr:
            send_reply(
                to_addr=to_addr,
                subject=pending.get("email_subject") or "Synapse: E*TRADE retry result",
                body=text,
                original_message_id=pending.get("email_message_id", ""),
                original_references=pending.get("email_references", ""),
            )
        else:
            logger.warning("Retry delivery skipped: retry_channel=email but no email_to/REPLY_TO_ADDRESS available")
    else:
        logger.warning("Retry delivery skipped: unrecognized or absent retry_channel %r on pending record", retry_channel)
