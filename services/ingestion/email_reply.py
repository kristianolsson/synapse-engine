"""
Email reply module.

Sends SMTP replies when the Gemini CLI produces non-empty output
(errors, clarifications, or query responses). Replies are hardcoded
to the configured service email for security.
"""

import logging
import smtplib
from email.mime.text import MIMEText

from . import config

logger = logging.getLogger(__name__)


def send_reply(
    to_addr: str,
    subject: str,
    body: str,
    original_message_id: str = "",
) -> bool:
    """
    Send an email reply via SMTP.

    Args:
        to_addr: Recipient (the original sender). Must be in ALLOWED_SENDERS.
        subject: Original email subject (will be prefixed with "Re: ").
        body: Reply body text (the Gemini CLI output).
        original_message_id: Message-ID of the original email for threading.

    Returns:
        True if sent successfully, False otherwise.
    """
    # Security: only reply to whitelisted addresses
    if to_addr.lower() not in config.ALLOWED_SENDERS:
        logger.warning("Refusing to reply to non-whitelisted address: %s", to_addr)
        return False

    reply_subject = subject if subject.startswith("Re:") else f"Re: {subject}"

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = config.EMAIL_ADDRESS
    msg["To"] = to_addr
    msg["Subject"] = reply_subject

    # Threading headers for proper Gmail conversation grouping
    if original_message_id:
        msg["In-Reply-To"] = original_message_id
        msg["References"] = original_message_id

    try:
        logger.info("Sending reply to %s: %r", to_addr, reply_subject)
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Reply sent successfully.")
        return True
    except Exception as e:
        logger.error("Failed to send reply: %s", e)
        return False
