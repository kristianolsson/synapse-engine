"""
Email reply module.

Sends SMTP replies when the Gemini CLI produces non-empty output
(errors, clarifications, or query responses). Replies are hardcoded
to the configured service email for security.
"""

import logging
import smtplib
from email.mime.text import MIMEText

from ... import config
from ...utils.stats_formatter import format_stats_email
from ...utils.task_formatter import TASK_PATTERN_OPEN, _hash_task

logger = logging.getLogger(__name__)


def send_reply(
    to_addr: str,
    subject: str,
    body: str,
    original_message_id: str = "",
    original_references: str = "",
    message_id: str = "",
    stats: dict = None,
) -> bool:
    """
    Send an email reply via SMTP.

    Args:
        to_addr: Recipient (the original sender). Must be in ALLOWED_SENDERS.
        subject: Original email subject (will be prefixed with "Re: ").
        body: Reply body text (the Gemini CLI output).
        original_message_id: Message-ID of the original email for threading.
        original_references: Existing References string from the original email.
        message_id: Explicit Message-ID to set for the outgoing email.
        stats: Optional execution statistics to append to the reply.

    Returns:
        True if sent successfully, False otherwise.
    """
    # Security: only reply to whitelisted addresses OR the configured reply-to address
    if (
        to_addr.lower() != config.REPLY_TO_ADDRESS
        and to_addr.lower() not in config.ALLOWED_SENDERS
    ):
        logger.warning("Refusing to reply to non-whitelisted address: %s", to_addr)
        return False

    # Only add 'Re: ' prefix if this is replying to an existing conversational thread
    if original_message_id:
        reply_subject = subject if subject.startswith("Re:") else f"Re: {subject}"
    else:
        reply_subject = subject

    # Format any tasks into mailto links before HTML line break conversion
    import urllib.parse
    
    def replacer(match):
        task_text = match.group(1).strip()
        if not task_text:
            return match.group(0)
            
        task_hash = _hash_task(task_text)
        task_text_encoded = urllib.parse.quote(task_text)
        mailto_url = f"mailto:{config.EMAIL_ADDRESS}?subject=DONE:{task_hash}&body={task_text_encoded}"
        btn_html = f'&nbsp;&nbsp;<a href="{mailto_url}" style="text-decoration:none;background-color:#f0f0f0;padding:2px 8px;border-radius:4px;border:1px solid #ccc;color:#333;font-size:0.9em;font-family:sans-serif;">✅ Complete</a>'
        return f"☐ {task_text}{btn_html}"

    body_with_links = TASK_PATTERN_OPEN.sub(replacer, body)
    final_body = body_with_links.replace('\n', '<br>') + format_stats_email(stats)
    msg = MIMEText(final_body, "html", "utf-8")
    msg["From"] = f"Synapse <{config.EMAIL_ADDRESS}>"
    msg["To"] = to_addr
    msg["Subject"] = reply_subject
    if message_id:
        msg["Message-ID"] = message_id

    # Threading headers for proper Gmail conversation grouping
    if original_message_id:
        msg["In-Reply-To"] = original_message_id

        # RFC 2822: References should be the original References + the original Message-ID
        if original_references:
            msg["References"] = f"{original_references} {original_message_id}"
        else:
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
