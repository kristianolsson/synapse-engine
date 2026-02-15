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
import signal
import sys
import tempfile
import time
from collections import deque
from email.message import EmailMessage

from imapclient import IMAPClient

from . import config
from .pipe import IncomingMessage, build_prompt, pipe_to_gemini

logger = logging.getLogger(__name__)


# ── Rate Limiter ────────────────────────────────────────────────────


class RateLimiter:
    """Sliding-window rate limiter."""

    def __init__(self, max_events: int, window_seconds: int):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        """Return True if the event is allowed, False if rate-limited."""
        now = time.time()
        # Purge expired timestamps
        while self._timestamps and (now - self._timestamps[0]) > self.window_seconds:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_events:
            return False
        self._timestamps.append(now)
        return True


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


def extract_images(msg: EmailMessage, temp_dir: str) -> list[str]:
    """
    Extract image attachments and inline images from an email.
    Saves them to temp_dir and returns a list of file paths.
    """
    image_paths = []
    if not msg.is_multipart():
        return image_paths

    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type and content_type.startswith("image/"):
            payload = part.get_payload(decode=True)
            if payload:
                filename = part.get_filename() or f"image_{len(image_paths)}.jpg"
                # Sanitize filename
                filename = re.sub(r"[^\w.\-]", "_", filename)
                filepath = os.path.join(temp_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(payload)
                image_paths.append(filepath)
                logger.info("Extracted image: %s (%d bytes)", filename, len(payload))

    return image_paths


# ── Message Processing ──────────────────────────────────────────────


def process_email(raw_bytes: bytes, temp_dir: str) -> tuple[bool, str]:
    """
    Parse a raw email, validate the sender, build a prompt, and pipe it.

    Returns (should_reply, reply_text).
    - should_reply=False, reply_text="" → success (silent)
    - should_reply=True, reply_text="..." → error/clarification to relay
    """
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    sender = extract_sender(msg)
    subject = msg.get("Subject", "(no subject)")

    logger.info("Processing email from=%s subject=%r", sender, subject)

    # Sender whitelist check
    if sender not in config.ALLOWED_SENDERS:
        logger.warning("Rejected email from unauthorized sender: %s", sender)
        return False, ""

    body = extract_text_body(msg)
    images = extract_images(msg, temp_dir)

    incoming = IncomingMessage(
        source_type="email",
        sender=sender,
        subject=subject,
        body=body,
        image_paths=images,
    )

    prompt = build_prompt(incoming)
    result = pipe_to_gemini(prompt)

    if result.success:
        return False, ""
    else:
        return True, result.output


# ── IMAP IDLE Loop ──────────────────────────────────────────────────


class EmailListener:
    """
    Persistent IMAP listener using IDLE for near-instant email processing.
    Handles reconnection with exponential backoff.
    """

    IDLE_TIMEOUT = 29 * 60  # 29 minutes per RFC 2177
    INITIAL_BACKOFF = 5  # seconds
    MAX_BACKOFF = 300  # 5 minutes

    def __init__(self):
        self.client: IMAPClient | None = None
        self.rate_limiter = RateLimiter(
            config.RATE_LIMIT_MAX, config.RATE_LIMIT_WINDOW_SECONDS
        )
        self._running = True
        self._backoff = self.INITIAL_BACKOFF

    def _connect(self):
        """Establish IMAP connection and select INBOX."""
        logger.info("Connecting to %s:%d...", config.IMAP_HOST, config.IMAP_PORT)
        self.client = IMAPClient(config.IMAP_HOST, port=config.IMAP_PORT, ssl=True)
        self.client.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
        self.client.select_folder("INBOX")
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
        """Fetch and process a batch of message UIDs."""
        for uid in msg_uids:
            if not self.rate_limiter.allow():
                logger.warning("Rate limit reached, skipping UID %s", uid)
                continue

            try:
                raw_data = self.client.fetch([uid], ["RFC822"])
                raw_bytes = raw_data[uid][b"RFC822"]

                with tempfile.TemporaryDirectory(prefix="lifeos_img_") as tmp:
                    should_reply, reply_text = process_email(raw_bytes, tmp)

                if should_reply and reply_text:
                    # Import here to avoid circular dependencies
                    from .email_reply import send_reply

                    raw_msg = email.message_from_bytes(
                        raw_bytes, policy=email.policy.default
                    )
                    send_reply(
                        to_addr=config.REPLY_TO_ADDRESS or extract_sender(raw_msg),
                        subject=raw_msg.get("Subject", ""),
                        body=reply_text,
                        original_message_id=raw_msg.get("Message-ID", ""),
                    )

                # Mark as read
                self.client.add_flags([uid], [b"\\Seen"])
                logger.info("UID %s processed and marked as read.", uid)

            except Exception as e:
                logger.error("Failed to process UID %s: %s", uid, e)

    def run(self):
        """Main loop: connect, IDLE, process, repeat."""
        logger.info("Starting email listener...")
        self._reconnect()

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


# ── Entry Point ─────────────────────────────────────────────────────


def main():
    """CLI entry point for the email listener service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    listener = EmailListener()

    def signal_handler(sig, frame):
        listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    listener.run()


if __name__ == "__main__":
    main()
