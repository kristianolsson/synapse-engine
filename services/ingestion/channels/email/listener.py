"""
IMAP email listener with IDLE support.

Connects to a Gmail IMAP server, watches for new emails using IDLE,
validates senders, extracts content/images, and pipes to the Gemini CLI.
"""

import email
import email.policy
import logging
import os
import re
from datetime import date
from typing import Optional
import time
from email.message import EmailMessage

from imapclient import IMAPClient

from ... import config
from ...core.pipe import IncomingMessage, sync_and_build_prompt, pipe_to_provider
from ...core.rate_limiter import RateLimiter
from ...tools.stocks import etrade_pin_auth
from ...core.session_manager import SessionManager, UserSession
from ...providers import PROVIDER_REGISTRY, user_visible_provider_names

logger = logging.getLogger(__name__)


# ── Email Parsing ───────────────────────────────────────────────────


def extract_sender(msg: EmailMessage) -> str:
    """Extract the bare email address from the From header."""
    from_header = msg.get("From", "")
    # Match the email inside angle brackets, or the whole string
    match = re.search(r"<(.+?)>", from_header)
    return (match.group(1) if match else from_header).strip().lower()


def extract_text_body(msg: EmailMessage) -> str:
    """Extract the plain-text body from an email, preferring text/plain."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try text/html
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


# Supported attachment content types
_ATTACHMENT_TYPES = (
    "image/",
    "application/pdf",
)


def extract_attachments(msg: EmailMessage) -> list[str]:
    """
    Extract image and PDF attachments from an email.
    Saves them to the vault's assets/ingestion/ folder and returns
    a list of absolute file paths.
    """
    paths = []
    if not msg.is_multipart():
        return paths

    assets_dir = os.path.join(config.VAULT_PATH, "assets", "ingestion")
    os.makedirs(assets_dir, exist_ok=True)
    today = date.today().isoformat()

    for part in msg.walk():
        content_type = part.get_content_type()
        if not content_type or not any(content_type.startswith(t) for t in _ATTACHMENT_TYPES):
            continue

        payload = part.get_payload(decode=True)
        if payload:
            raw_name = part.get_filename() or f"attachment_{len(paths)}"
            raw_name = re.sub(r"[^\w.\-]", "_", raw_name)
            stem, ext = os.path.splitext(raw_name)
            filename = f"{today}_{stem}{ext}"

            # Handle name conflicts with _1, _2, etc.
            filepath = os.path.join(assets_dir, filename)
            counter = 1
            while os.path.exists(filepath):
                filename = f"{today}_{stem}_{counter}{ext}"
                filepath = os.path.join(assets_dir, filename)
                counter += 1

            with open(filepath, "wb") as f:
                f.write(payload)
            paths.append(filepath)
            logger.info("Extracted attachment: %s (%d bytes)", filename, len(payload))

    return paths


def _send_reply_with_retry(**kwargs) -> bool:
    """Send an email reply, retrying once so a transient SMTP blip doesn't
    silently drop it. Logs at CRITICAL if both attempts fail, since that
    means the reply is lost with no other notification channel."""
    from .reply import send_reply

    if send_reply(**kwargs):
        return True

    logger.warning("Reply send failed, retrying once...")
    if send_reply(**kwargs):
        return True

    logger.critical(
        "Reply delivery FAILED after retry — to=%s subject=%r body=%.500r",
        kwargs.get("to_addr"), kwargs.get("subject"), kwargs.get("body"),
    )
    return False


# ── Message Processing ──────────────────────────────────────────────


def process_email(
    raw_bytes: bytes, session_manager: SessionManager
) -> tuple[bool, str, Optional[dict], Optional[UserSession]]:
    """
    Parse a raw email, validate the sender, build a prompt, and pipe it.

    Returns (should_reply, reply_text, stats, session).
    - should_reply=False, reply_text="", stats=None → success (silent)
    - should_reply=True, reply_text="...", stats={...} → error/clarification to relay
    - stats is the raw, ungated dict; pass it to send_reply along with
      session so send_reply's own gating decides whether to show it.
    """
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    sender = extract_sender(msg)
    subject = msg.get("Subject", "(no subject)")

    in_reply_to = msg.get("In-Reply-To", "").strip()
    references = msg.get("References", "").strip()
    message_id = msg.get("Message-ID", "").strip()

    # Session grouping logic:
    # 1. If 'References' exists, the root thread ID is usually the first ID in the list.
    # 2. If no 'References', fallback to 'In-Reply-To'.
    # 3. If neither, it's a completely new thread, so use its own 'Message-ID'.
    if references:
        session_key = references.split()[0]
    elif in_reply_to:
        session_key = in_reply_to
    else:
        session_key = message_id

    # Bound to session_key (per-thread) for conversation continuity, but to
    # sender (per-address) for the /stats preference — one person can have
    # many threads, and the preference should follow them, not the thread.
    session = UserSession(session_manager, session_key, stats_key=sender)

    logger.info("Processing email from=%s subject=%r", sender, subject)

    # Sender whitelist check
    if sender not in config.ALLOWED_SENDERS:
        logger.warning("Rejected email from unauthorized sender: %s", sender)
        if config.REPLY_TO_ADDRESS:
            _send_reply_with_retry(
                to_addr=config.REPLY_TO_ADDRESS,
                subject="Synapse: Rejected email",
                body=f"Rejected email from unauthorized sender.\n\nFrom: {sender}\nSubject: {subject}",
                original_message_id=msg.get("Message-ID", ""),
                original_references=msg.get("References", ""),
            )
        return False, "", None, None

    body = extract_text_body(msg)

    # --- PENDING E*TRADE RE-AUTH ---
    # Only a genuine reply to the prompt WE sent (matched by Message-ID,
    # via In-Reply-To/References) counts as the pasted verification code —
    # this must never fall through to the general assistant pipeline
    # below, whether the prompt was sent by a scheduled job's auth
    # fallback (etrade_cli.py) or a future manual trigger.
    pending_etrade = etrade_pin_auth.load_pending()
    if pending_etrade and pending_etrade.get("channel") == "email":
        prompt_id = pending_etrade.get("prompt_message_id", "")
        if prompt_id and (in_reply_to == prompt_id or prompt_id in references.split()):
            code = body.strip().splitlines()[0].strip() if body.strip() else ""
            if not code:
                return True, "⚠️ Could not find a verification code in that reply.", None, session

            # Atomically consume the pending request before doing anything
            # slow (network OAuth exchange, then a full options-bot replay)
            # — if the user resent this same code, or two replies raced in,
            # only the first claim can win. See claim_pending()'s docstring
            # for the duplicate-run bug this prevents.
            claimed = etrade_pin_auth.claim_pending()
            if claimed is None or claimed.get("prompt_message_id") != prompt_id:
                return True, "This E*TRADE login was already completed.", None, session

            try:
                tokens = etrade_pin_auth.finish_pin_auth(
                    claimed, code, config.ETRADE_CONSUMER_KEY, config.ETRADE_CONSUMER_SECRET
                )
                etrade_pin_auth.save_access_token(
                    tokens["access_token"],
                    tokens["access_token_secret"],
                    sandbox=config.ETRADE_MODE.lower() == "sandbox",
                )
            except Exception as e:
                # Put it back rather than discarding it — a mistyped code
                # shouldn't cost the reminder_task/session_key correlation;
                # a follow-up reply with the correct code should still work.
                etrade_pin_auth.restore_pending(claimed)
                return True, f"❌ E*TRADE login failed: {e}\n\nReply again with the correct code.", None, session
            try:
                etrade_pin_auth.complete_and_maybe_retry(claimed, session_manager)
            except Exception as e:
                logger.exception("E*TRADE retry failed after successful PIN completion: %s", e)
            return True, "✅ E*TRADE login successful.", None, session

    # --- ONE-TAP COMPLETION (MAILTO LINKS) ---
    if subject.startswith("DONE:") or subject.startswith("UNDO:"):
        is_done = subject.startswith("DONE:")
        task_hash = subject.split(":", 1)[1].strip()

        # Manual replies that keep the DONE/UNDO subject but add free text
        # above quoted history: recover the exact task line by hash match.
        from ...utils.task_formatter import recover_task_from_callback
        task_text = recover_task_from_callback(body, task_hash)

        if not task_text:
            # Mailto-link taps: the body is just the task text. Mail clients
            # hard-wrap long plain-text lines (e.g. long URLs), splitting it
            # across multiple physical lines, so collapse all whitespace/
            # newlines back into a single line instead of taking line one.
            task_text = re.sub(r"\s+", " ", body).strip()

        if not task_text:
            return True, "⚠️ Could not identify this task from the email payload. Please complete it manually.", None, session
            
        if is_done:
            prompt = f"Mark the following task as completed: {task_text}"
        else:
            prompt = f"Mark the following task as NOT completed (undo): {task_text}"
            
        logger.info("Intercepted email mailto command: %s", prompt)
        
        # Process as a direct command sent from the user.
        incoming = IncomingMessage(
            source_type="email",
            sender=sender,
            subject="(Task Completion)",
            body=prompt,
        )
        full_prompt = sync_and_build_prompt(incoming)
        session_id = session.session_id
        result = pipe_to_provider(full_prompt, session_id=session_id)

        if result.session_id:
            session.save(result.session_id)

        if result.is_error:
            logger.error("Task completion failed for '%s': %s", task_text, result.output)
            return True, f"⚠️ Failed to complete task: {task_text}\n\nError: {result.output}", result.stats, session

        if result.requires_reply:
            return True, result.output, result.stats, session

        # Success - no need to reply to a button click
        return False, "", result.stats, session

    if body.strip() in ("/new", "/clear"):
        if session.clear():
            return True, "Session cleared. Starting a fresh context.", None, session
        else:
            return True, "No active session to clear.", None, session

    # /stats on|off command
    stripped_body = body.strip().lower()
    if stripped_body in ("/stats on", "/stats off"):
        enabled = stripped_body == "/stats on"
        session.set_stats_enabled(enabled)
        label = "on" if enabled else "off"
        return True, f"Stats display turned {label}.", None, session

    # /provider command
    if stripped_body.startswith("/provider"):
        parts = stripped_body.split()
        if len(parts) == 2:
            requested = parts[1]
            if requested in PROVIDER_REGISTRY:
                config.set_ai_provider(requested)
                return True, f"Switched to {requested} provider.", None, session
            else:
                return True, f"Unknown provider: {requested}. Options: {', '.join(user_visible_provider_names())}", None, session
        else:
            current = config.get_ai_provider()
            return True, f"Current provider: {current}. Usage: /provider <{'|'.join(user_visible_provider_names())}>", None, session

    attachments = extract_attachments(msg)

    incoming = IncomingMessage(
        source_type="email",
        sender=sender,
        subject=subject,
        body=body,
        attachment_paths=attachments,
    )

    prompt = sync_and_build_prompt(incoming)
    session_id = session.session_id
    extra_env = {
        "SYNAPSE_SESSION_KEY": session_key,
        "SYNAPSE_SESSION_ID": session_id or "",
        "SYNAPSE_CHANNEL": "email",
        "SYNAPSE_EMAIL_TO": sender,
        "SYNAPSE_EMAIL_SUBJECT": subject,
        "SYNAPSE_EMAIL_MESSAGE_ID": message_id,
        "SYNAPSE_EMAIL_REFERENCES": references,
    }
    result = pipe_to_provider(prompt, session_id=session_id, extra_env=extra_env)

    if result.session_id:
        session.save(result.session_id)
        etrade_pin_auth.backfill_session_id(session_key, result.session_id)

    if result.requires_reply:
        return True, result.output, result.stats, session
    else:
        return False, "", result.stats, session


# ── IMAP IDLE Loop ──────────────────────────────────────────────────


class EmailListener:
    """
    Persistent IMAP listener using IDLE for near-instant email processing.
    Handles reconnection with exponential backoff.
    """

    IDLE_TIMEOUT = 29 * 60  # 29 minutes per RFC 2177
    INITIAL_BACKOFF = 5  # seconds
    MAX_BACKOFF = 300  # 5 minutes

    def __init__(self, rate_limiter: RateLimiter, session_manager: SessionManager):
        # Both must be the caller's shared, live instances (constructed once
        # in main.py) — not fresh ones. RateLimiter is documented as shared
        # across channels; SessionManager's per-user /stats preferences are
        # in-memory only, so a fresh instance would never see toggles set on
        # another channel's instance.
        self.client: Optional[IMAPClient] = None
        self.rate_limiter = rate_limiter
        self.session_manager = session_manager
        self._running = True
        self._backoff = self.INITIAL_BACKOFF

    def _connect(self):
        """Establish IMAP connection and select INBOX."""
        logger.info("Connecting to %s:%d...", config.IMAP_HOST, config.IMAP_PORT)
        self.client = IMAPClient(config.IMAP_HOST, port=config.IMAP_PORT, ssl=True)
        self.client.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
        self.client.select_folder("INBOX")

        # Detect the "All Mail" folder by its \All flag (locale-independent)
        self._archive_folder = None
        for flags, _delim, name in self.client.list_folders():
            if b"\\All" in flags:
                self._archive_folder = name
                break
        if self._archive_folder:
            logger.info("Archive folder: %s", self._archive_folder)
        else:
            logger.warning("No archive folder found, emails will only be marked as read.")

        logger.info("Connected and INBOX selected.")
        self._backoff = self.INITIAL_BACKOFF  # Reset on successful connect

    def _reconnect(self):
        """Reconnect with exponential backoff."""
        while self._running:
            try:
                if self.client:
                    try:
                        self.client.logout()
                    except Exception:
                        pass
                self._connect()
                return
            except Exception as e:
                logger.error(
                    "Connection failed: %s. Retrying in %ds...", e, self._backoff
                )
                time.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)

    def _fetch_and_process(self, msg_uids: list[int]):
        """Fetch and process unread messages one at a time.

        Re-searches for UNSEEN after each message since archive/move
        can invalidate remaining UIDs.
        """
        while msg_uids:
            uid = msg_uids[0]

            if not self.rate_limiter.allow():
                logger.warning("Rate limit reached, skipping remaining messages")
                # Reply to the current message so the sender knows
                try:
                    raw_data = self.client.fetch([uid], ["RFC822"])
                    raw_bytes = raw_data[uid][b"RFC822"]
                    raw_msg = email.message_from_bytes(
                        raw_bytes, policy=email.policy.default
                    )
                    _send_reply_with_retry(
                        to_addr=config.REPLY_TO_ADDRESS,
                        subject=raw_msg.get("Subject", ""),
                        body="⚠️ Rate limit reached. Please try again in a minute.",
                        original_message_id=raw_msg.get("Message-ID", ""),
                        original_references=raw_msg.get("References", ""),
                    )
                except Exception as e:
                    logger.error("Failed to send rate limit reply: %s", e)
                break

            try:
                raw_data = self.client.fetch([uid], ["RFC822"])
                raw_bytes = raw_data[uid][b"RFC822"]

                should_reply, reply_text, stats, session = process_email(raw_bytes, self.session_manager)

                if should_reply and reply_text:
                    raw_msg = email.message_from_bytes(
                        raw_bytes, policy=email.policy.default
                    )
                    _send_reply_with_retry(
                        to_addr=config.REPLY_TO_ADDRESS,
                        subject=raw_msg.get("Subject", ""),
                        body=reply_text,
                        original_message_id=raw_msg.get("Message-ID", ""),
                        original_references=raw_msg.get("References", ""),
                        stats=stats,
                        session=session,
                    )

                # Mark as read and archive (move out of INBOX)
                self.client.add_flags([uid], [b"\\Seen"])
                if self._archive_folder:
                    try:
                        self.client.move([uid], self._archive_folder)
                    except Exception:
                        self.client.copy([uid], self._archive_folder)
                        self.client.delete_messages([uid])
                        self.client.expunge([uid])
                logger.info("UID %s processed, marked as read, and archived.", uid)

            except Exception as e:
                logger.error("Failed to process UID %s: %s", uid, e)

            # Re-search for remaining UNSEEN messages (UIDs may have changed)
            msg_uids = self.client.search(["UNSEEN"])

    def run(self):
        """Main loop: connect, IDLE, process, repeat."""
        logger.info("Starting email listener...")
        self._reconnect()

        # Process any unread messages that arrived while the service was down
        backlog = self.client.search(["UNSEEN"])
        if backlog:
            logger.info("Found %d unread message(s) on startup.", len(backlog))
            self._fetch_and_process(backlog)

        while self._running:
            try:
                # Start IDLE
                self.client.idle()
                logger.debug("IDLE started, waiting for events...")

                responses = self.client.idle_check(timeout=self.IDLE_TIMEOUT)
                self.client.idle_done()

                if not responses:
                    # Timeout — IDLE needs to be re-issued to keep alive
                    logger.debug("IDLE timeout, re-issuing...")
                    continue

                # Check for new messages (EXISTS responses)
                new_uids = self.client.search(["UNSEEN"])
                if new_uids:
                    logger.info("Found %d new message(s).", len(new_uids))
                    self._fetch_and_process(new_uids)

            except Exception as e:
                logger.error("IDLE loop error: %s. Reconnecting...", e)
                self._reconnect()

    def stop(self):
        """Gracefully stop the listener."""
        logger.info("Stopping email listener...")
        self._running = False
        if self.client:
            try:
                self.client.logout()
            except Exception:
                pass
