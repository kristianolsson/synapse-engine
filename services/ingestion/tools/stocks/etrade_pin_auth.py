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

import fcntl
import json
import logging
import time
from pathlib import Path
from typing import Optional

from authlib.integrations.requests_client import OAuth1Session

logger = logging.getLogger(__name__)

PENDING_FILE = Path.home() / ".etrade_pending_auth.json"
PENDING_LOCK_FILE = Path(str(PENDING_FILE) + ".lock")
PENDING_TTL_SECONDS = 120 * 60
TOKEN_FILE = Path.home() / ".etrade_tokens"


def _read_pending_unlocked() -> Optional[dict]:
    """Read and expiry-check the pending file with no locking. Only safe for
    callers that don't need exclusivity — see load_pending() vs.
    claim_pending()."""
    if not PENDING_FILE.exists():
        return None
    try:
        data = json.loads(PENDING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("created_at", 0) > PENDING_TTL_SECONDS:
        PENDING_FILE.unlink(missing_ok=True)
        return None
    return data


def load_pending() -> Optional[dict]:
    """Return the pending auth request, or None if there isn't one or it
    expired. Read-only, not locked — fine for a non-destructive check (e.g.
    etrade_cli.py asking "is one already pending?"), but two callers racing
    to *complete* the same request must use claim_pending() instead: this
    function alone lets both see the same still-valid pending before either
    finishes handling it (confirmed root cause of a duplicate options-bot
    run + duplicate reply after the user resent a verification code)."""
    return _read_pending_unlocked()


def claim_pending() -> Optional[dict]:
    """Atomically read-and-consume the pending auth request so that of
    several near-simultaneous completion attempts (e.g. the user resending
    the verification code, or double-tapping send), only the first can ever
    proceed — the rest see no pending request at all and should treat that
    as "already completed" rather than redoing the token exchange and
    retry. Callers must still do their own channel/prompt-id match check on
    the returned dict first (this only guarantees at most one winner across
    concurrent callers, not that the request is the one *this* caller
    expects)."""
    with open(PENDING_LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            pending = _read_pending_unlocked()
            if pending is None:
                return None
            PENDING_FILE.unlink(missing_ok=True)
            return pending
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def restore_pending(pending: dict) -> None:
    """Put a claimed pending request back after a failed completion attempt
    (e.g. a mistyped verification code), so a follow-up reply with the
    correct code can still complete it against the same oauth_token —
    preserving the reminder_task/session_key retry correlation instead of
    silently losing it and forcing a fresh /update-etrade-auth run (which
    starts a new, uncorrelated pending). If the underlying request token
    itself was the problem (already consumed, expired at E*TRADE's end),
    the next attempt will just fail the same way and the user falls back to
    /update-etrade-auth manually — restoring is harmless either way."""
    with open(PENDING_LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            PENDING_FILE.write_text(json.dumps(pending))
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def clear_pending() -> None:
    PENDING_FILE.unlink(missing_ok=True)


def _retry_identity(entry: dict) -> Optional[tuple]:
    """Stable identity for a queued retry entry, used by queue_retry() to
    collapse repeat failures of the same logical job instead of queuing a
    duplicate replay of it. Keyed by whichever correlation field the entry
    carries (see _fallback_to_pin_auth): session_key for the interactive
    case (a retried tool call within the same conversation turn resumes
    the same session), reminder_task for the scheduled case (a retried
    tool call within the same reminder firing replays the same task
    text). Returns None for an entry with neither — nothing to dedupe
    against, so it's always queued."""
    if entry.get("session_key"):
        return ("session", entry["session_key"])
    if entry.get("reminder_task"):
        return ("reminder", entry["reminder_task"])
    return None


def queue_retry(entry: dict) -> None:
    """Append a retry-correlation entry (session-resume or reminder-replay
    shape — see complete_and_maybe_retry) onto the pending request's
    `retries` list. Several jobs failing while one E*TRADE login is
    already in flight all queue onto the same pending request instead of
    each starting (or being locked out of) their own — completing that
    one login retries all of them. No-op if there's no pending request
    (e.g. it expired first).

    Collapses onto an existing entry with the same _retry_identity()
    instead of appending a second one — a single logical job (one
    reminder firing, one conversation turn) can hit this fallback more
    than once, e.g. an agent retrying its own failed tool call, and each
    such retry must not queue its own extra replay (confirmed root cause
    of a real double-email incident: one options-bot reminder firing
    queued two identical entries, and completing the one login replayed
    the same scan twice)."""
    pending = load_pending()
    if pending is None:
        return
    retries = pending.setdefault("retries", [])
    identity = _retry_identity(entry)
    if identity is not None and any(_retry_identity(e) == identity for e in retries):
        logger.info("queue_retry: duplicate retry for %r already queued — collapsing", identity)
        return
    retries.append(entry)
    PENDING_FILE.write_text(json.dumps(pending))


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
    """If a queued retry entry (see queue_retry) is waiting on this exact
    session_key but didn't yet know its session_id (it was the first
    message in a brand-new thread when the auth fallback fired), fill it
    in now that the provider has assigned one. No-op otherwise."""
    if not session_id:
        return
    pending = load_pending()
    if pending is None:
        return
    changed = False
    for entry in pending.get("retries", []):
        if entry.get("session_key") == session_key and not entry.get("session_id"):
            entry["session_id"] = session_id
            changed = True
    if changed:
        PENDING_FILE.write_text(json.dumps(pending))


def _fetch_request_token(consumer_key: str, consumer_secret: str) -> dict:
    """The actual E*TRADE network call shared by start_pin_auth() (email
    path — fetched eagerly) and activate_pending_auth() (Telegram path —
    fetched only once the user is actually ready), so the login URL's
    validity window starts ticking as late as possible."""
    client = OAuth1Session(client_id=consumer_key, client_secret=consumer_secret, redirect_uri="oob")
    request_token = client.fetch_request_token(
        url="https://api.etrade.com/oauth/request_token", params={"format": "json"}
    )
    authorize_url = (
        f"https://us.etrade.com/e/t/etws/authorize?key={consumer_key}&token={request_token['oauth_token']}"
    )
    return {
        "oauth_token": request_token["oauth_token"],
        "oauth_token_secret": request_token["oauth_token_secret"],
        "authorize_url": authorize_url,
    }


def start_pin_auth(consumer_key: str, consumer_secret: str) -> dict:
    """Fetch a request token + authorize URL from E*TRADE and persist
    pending state for finish_pin_auth to complete later, possibly from a
    different process. Call mark_prompt_sent() once the prompt has
    actually been delivered, to record which channel/message to match
    the reply against.

    Used for channels with no interactive "I'm ready" step (email) — for
    Telegram, use create_pending_request() + activate_pending_auth()
    instead, so the request token isn't fetched until the user actually
    taps the button.

    Returns the pending dict, including 'authorize_url' to send to the user.
    """
    pending = {
        **_fetch_request_token(consumer_key, consumer_secret),
        "created_at": time.time(),
        "activated": True,
        "retries": [],
    }
    PENDING_FILE.write_text(json.dumps(pending))
    PENDING_FILE.chmod(0o600)
    return pending


def create_pending_request() -> dict:
    """Write a bare pending record with no E*TRADE call yet — the
    Telegram counterpart to start_pin_auth(). The real request-token
    fetch is deferred to activate_pending_auth(), called once the user
    taps "I'm ready" (or runs /update-etrade-auth), so E*TRADE's own
    short-lived token doesn't start ticking before anyone's looking at
    it."""
    pending = {"created_at": time.time(), "activated": False, "retries": []}
    PENDING_FILE.write_text(json.dumps(pending))
    PENDING_FILE.chmod(0o600)
    return pending


def activate_pending_auth(consumer_key: str, consumer_secret: str) -> Optional[dict]:
    """Fetch the real E*TRADE request token/authorize URL now and merge it
    onto the existing pending record — the deferred half of
    create_pending_request(), called once the user is actually ready.
    Resets `created_at` so PENDING_TTL_SECONDS restarts from the moment
    the real token was issued (the moment its own clock matters) rather
    than from whenever the original prompt was sent.

    Returns None if there's no pending request to activate (expired, or
    already claimed/completed) — the caller should treat that as
    "nothing to do." An already-activated request is returned unchanged
    (idempotent — a stale button tap, or a manual /update-etrade-auth run
    after the other route already activated it, must not fetch a second,
    wasted token)."""
    pending = load_pending()
    if pending is None:
        return None
    if pending.get("activated"):
        return pending
    pending.update(_fetch_request_token(consumer_key, consumer_secret))
    pending["created_at"] = time.time()
    pending["activated"] = True
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


def _reminder_stats_sender(entry: dict, retry_channel: str) -> str:
    """Reminders never store a session/user key (see module docstring on
    complete_and_maybe_retry), so reconstruct the same sender identity
    scheduler.py's _handle_work_reminder uses to look up stats prefs."""
    if retry_channel == "email":
        return entry.get("email_to", "")
    if retry_channel == "telegram":
        from ... import config
        return str(config.TELEGRAM_ALLOWED_USER_IDS[0]) if config.TELEGRAM_ALLOWED_USER_IDS else "system"
    return "system"


def complete_and_maybe_retry(pending: dict, session_manager) -> None:
    """After a successful PIN completion, retry every job that queued
    itself onto this pending request while the login was in flight (see
    queue_retry) — each entry independently either resumes the exact
    session that failed (interactive case) or replays a reminder task
    fresh (scheduled case). No-op if `retries` is empty (e.g. a manual
    /update-etrade-auth run has nothing queued). One entry failing
    doesn't stop the rest.

    `session_manager` must be the caller's live instance (not a fresh
    SessionManager()) — per-user stats prefs (`set_stats_enabled`) are
    in-memory only, so a new instance would never see them and stats
    would silently always fall back to the config default."""
    for entry in pending.get("retries", []):
        try:
            _retry_one(entry, pending, session_manager)
        except Exception:
            logger.exception("complete_and_maybe_retry: failed to replay queued retry %r", entry)


def _retry_one(entry: dict, pending: dict, session_manager) -> None:
    session_key = entry.get("session_key")
    reminder_task = entry.get("reminder_task")

    # A queued entry rarely carries its own chat_id (only interactive
    # SYNAPSE_CHAT_ID captures do; reminders never do — see
    # _fallback_to_pin_auth) — fall back to the chat the login prompt
    # itself was sent to, the single-owner chat both cases actually share.
    entry = {**entry, "chat_id": entry.get("chat_id") or pending.get("chat_id")}

    if session_key:
        logger.info("complete_and_maybe_retry: resuming session %s (session-resume branch)", session_key)
        from ...core.pipe import pipe_to_provider, sync_and_build_prompt, IncomingMessage

        failed_command = entry.get("failed_command", "")
        retry_channel = entry.get("retry_channel", "system")
        # Route through the same envelope every real message gets (Type/
        # Sender/Context/Current Time) instead of a bare string — an
        # unattributed instruction arriving mid-session reads as a prompt
        # injection attempt, and got correctly flagged as one in practice.
        sender = entry.get("email_to") if retry_channel == "email" else session_key
        retry_text = (
            "E*TRADE authentication just completed successfully. You were "
            f"blocked earlier in this conversation when running `{failed_command}` "
            "— retry that now and complete my full original request."
        )
        from ...core.session_manager import UserSession
        # session_key is the email listener's own thread key when
        # retry_channel is "email" (see channels/email/listener.py's
        # SYNAPSE_SESSION_KEY correlation) — same daily_reset=False rationale
        # applies: threads span multiple calendar days, so this session
        # should live purely by SESSION_TTL_MINUTES.
        session = UserSession(session_manager, session_key, stats_key=sender, daily_reset=retry_channel != "email")
        retry_prompt = sync_and_build_prompt(IncomingMessage(source_type=retry_channel, sender=sender or "system", body=retry_text))
        result = pipe_to_provider(retry_prompt, session_id=entry.get("session_id") or None)
        if result.session_id:
            session.save(result.session_id)
        text = result.output or "✓"
        _deliver_retry_result(entry, text, stats=result.stats, session=session)

    elif reminder_task:
        logger.info("complete_and_maybe_retry: replaying reminder task %r (reminder-replay branch)", reminder_task)
        from ...core.pipe import pipe_to_provider, sync_and_build_prompt, IncomingMessage

        # Deliberately simplified compared to scheduler.py's
        # _handle_work_reminder: no quota-fallback provider switch, no
        # session-save-for-reply-continuation, no form-keyboard rendering.
        # Acceptable for a degraded auth-recovery path — this isn't meant
        # to fully replicate the scheduler's normal delivery, just get the
        # result back to the user once auth is unblocked.
        incoming = IncomingMessage(
            source_type="scheduled_work",
            # Distinct from the plain "system" sender used elsewhere (e.g.
            # _reminder_stats_sender's stats-key fallback) — this one is
            # purely the "Sender:" line a log-reading agent sees (pipe.py's
            # sync_and_build_prompt), so it should self-explain why a
            # reminder fired under Sender: system shortly after the same
            # task's normal Sender: <email/telegram id> attempt failed,
            # without needing to cross-reference ARCHITECTURE.md.
            sender="system (retry after E*TRADE re-auth)",
            subject="Scheduled Work Task",
            body=reminder_task,
        )
        prompt = sync_and_build_prompt(incoming)
        result = pipe_to_provider(prompt, model="work")
        text = result.output or f"✓ Scheduled task completed: {reminder_task}"
        retry_channel = entry.get("retry_channel", "system")
        stats_sender = _reminder_stats_sender(entry, retry_channel)
        from ...core.session_manager import UserSession
        session = UserSession(session_manager, stats_sender)
        _deliver_retry_result(entry, text, stats=result.stats, session=session)


def _deliver_retry_result(entry: dict, text: str, stats: Optional[dict] = None, session=None) -> None:
    """Send one queued retry entry's result to wherever its original
    request came from (not necessarily wherever the PIN reply was
    completed). `stats` is the raw provider-result dict and `session` the
    UserSession handle to gate it on — both send_reply() (email) and
    send_telegram_message() (telegram) do that gating plus their own
    formatting internally, so `stats` must not already be baked into
    `text` and gating must not be done again here."""
    retry_channel = entry.get("retry_channel")
    if retry_channel == "telegram":
        from ...channels.telegram.sender import send_telegram_message
        from ...utils.html_utils import sanitize_telegram_html

        # Telegram-delivered retries prefer the dedicated retry_chat_id
        # (the chat of whoever's original REQUEST is being retried) but
        # fall back to the plain chat_id field, which _retry_one() already
        # merged in from the pending's own chat_id — always populated for
        # the scheduled-reminder replay case (no SYNAPSE_CHAT_ID/
        # retry_chat_id is ever set for reminders).
        chat_id = entry.get("retry_chat_id") or entry.get("chat_id")
        if chat_id:
            send_telegram_message(chat_id, sanitize_telegram_html(text), stats=stats, session=session)
        else:
            logger.warning("Retry delivery skipped: retry_channel=telegram but no chat_id/retry_chat_id on entry")
    elif retry_channel == "email":
        from ... import config
        from ...channels.email.reply import send_reply

        to_addr = entry.get("email_to") or config.REPLY_TO_ADDRESS
        if to_addr:
            send_reply(
                to_addr=to_addr,
                subject=entry.get("email_subject") or "Synapse: E*TRADE retry result",
                body=text,
                original_message_id=entry.get("email_message_id", ""),
                original_references=entry.get("email_references", ""),
                stats=stats,
                session=session,
            )
        else:
            logger.warning("Retry delivery skipped: retry_channel=email but no email_to/REPLY_TO_ADDRESS available")
    else:
        logger.warning("Retry delivery skipped: unrecognized or absent retry_channel %r on pending record", retry_channel)
