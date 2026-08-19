"""Tests for the email listener module."""

import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from unittest.mock import patch, MagicMock


from services.ingestion.channels.email.listener import (
    EmailListener,
    extract_sender,
    extract_text_body,
    extract_attachments,
    process_email,
    _send_reply_with_retry,
)
from services.ingestion.core.session_manager import SessionManager
from services.ingestion.core.rate_limiter import RateLimiter


# ── RateLimiter tests ───────────────────────────────────────────────


class TestRateLimiter:
    def test_allows_within_limit(self):
        rl = RateLimiter(max_events=3, window_seconds=60)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is True

    def test_blocks_over_limit(self):
        rl = RateLimiter(max_events=2, window_seconds=60)
        assert rl.allow() is True
        assert rl.allow() is True
        assert rl.allow() is False

    @patch("services.ingestion.core.rate_limiter.time")
    def test_window_expiry(self, mock_time):
        rl = RateLimiter(max_events=1, window_seconds=10)
        mock_time.time.return_value = 100.0
        assert rl.allow() is True
        assert rl.allow() is False
        # Fast-forward past the window
        mock_time.time.return_value = 111.0
        assert rl.allow() is True


# ── Email Parsing tests ─────────────────────────────────────────────


def _make_simple_email(from_addr="test@example.com", subject="Hello", body="World"):
    """Helper to create a simple email as raw bytes."""
    msg = MIMEText(body)
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["To"] = "inbox@lifeos.com"
    return msg.as_bytes()


def _make_email_with_display_name():
    msg = MIMEText("body")
    msg["From"] = "John Doe <john@example.com>"
    msg["Subject"] = "Test"
    return msg.as_bytes()


class TestExtractSender:
    def test_bare_address(self):
        msg = email.message_from_bytes(_make_simple_email("user@test.com"))
        assert extract_sender(msg) == "user@test.com"

    def test_display_name(self):
        msg = email.message_from_bytes(_make_email_with_display_name())
        assert extract_sender(msg) == "john@example.com"

    def test_case_insensitive(self):
        msg = email.message_from_bytes(_make_simple_email("USER@Test.COM"))
        assert extract_sender(msg) == "user@test.com"


class TestExtractTextBody:
    def test_plain_text(self):
        raw = _make_simple_email(body="Hello world")
        msg = email.message_from_bytes(raw)
        assert extract_text_body(msg) == "Hello world"

    def test_multipart(self):
        msg = MIMEMultipart()
        msg["From"] = "test@example.com"
        msg.attach(MIMEText("Plain text body", "plain"))
        msg.attach(MIMEText("<p>HTML body</p>", "html"))
        parsed = email.message_from_bytes(msg.as_bytes())
        assert "Plain text body" in extract_text_body(parsed)


class TestExtractAttachments:
    def test_no_attachments(self):
        raw = _make_simple_email()
        msg = email.message_from_bytes(raw)
        attachments = extract_attachments(msg)
        assert attachments == []

    @patch("services.ingestion.channels.email.listener.config")
    def test_with_image_attachment(self, mock_config, tmp_path):
        mock_config.VAULT_PATH = str(tmp_path)
        msg = MIMEMultipart()
        msg["From"] = "test@example.com"
        msg.attach(MIMEText("See attached", "plain"))
        # Create a fake image
        img = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, name="test.png")
        msg.attach(img)
        parsed = email.message_from_bytes(msg.as_bytes())
        attachments = extract_attachments(parsed)
        assert len(attachments) == 1
        assert "test.png" in attachments[0]

    @patch("services.ingestion.channels.email.listener.config")
    def test_with_pdf_attachment(self, mock_config, tmp_path):
        from email.mime.base import MIMEBase
        from email import encoders
        mock_config.VAULT_PATH = str(tmp_path)
        msg = MIMEMultipart()
        msg["From"] = "test@example.com"
        msg.attach(MIMEText("See attached PDF", "plain"))
        pdf_part = MIMEBase("application", "pdf")
        pdf_part.set_payload(b"%PDF-1.4 fake content")
        encoders.encode_base64(pdf_part)
        pdf_part.add_header("Content-Disposition", "attachment", filename="report.pdf")
        msg.attach(pdf_part)
        parsed = email.message_from_bytes(msg.as_bytes())
        attachments = extract_attachments(parsed)
        assert len(attachments) == 1
        assert "report.pdf" in attachments[0]


# ── process_email tests ─────────────────────────────────────────────


class TestProcessEmail:
    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_rejects_unauthorized_sender(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["allowed@example.com"]
        raw = _make_simple_email(from_addr="hacker@evil.com")
        sm = MagicMock(spec=SessionManager)
        should_reply, text, stats = process_email(raw, sm)
        assert should_reply is False
        assert stats is None
        mock_pipe.assert_not_called()

    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_success_silent(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)
        raw = _make_simple_email(from_addr="user@example.com", body="Buy milk")
        sm = MagicMock(spec=SessionManager)
        should_reply, text, stats = process_email(raw, sm)
        assert should_reply is False
        assert text == ""
        assert stats is None

    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_error_triggers_reply(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=True, output="Repo is locked", session_id="", stats={"models": {}})
        raw = _make_simple_email(from_addr="user@example.com", body="Add todo")
        sm = MagicMock(spec=SessionManager)
        should_reply, text, stats = process_email(raw, sm)
        assert should_reply is True
        assert "Repo is locked" in text
        assert stats is not None

    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_process_done_subject_from_body(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)
        
        # Simulating mailto payload where task text is in the body
        raw = _make_simple_email(from_addr="user@example.com", subject="DONE:abc12345", body="Buy groceries")
        sm = MagicMock(spec=SessionManager)
        
        should_reply, text, stats = process_email(raw, sm)
        
        assert should_reply is False
        assert mock_pipe.call_count == 1
        args = mock_pipe.call_args[0]
        assert "Mark the following task as completed: Buy groceries" in args[0]
        
    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_process_undo_subject_from_history(self, mock_config, mock_pipe):
        from services.ingestion.utils.task_formatter import _hash_task
        task_hash = _hash_task("Fix the fence")
        
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)
        
        # Simulating normal reply where user kept subject but body has quoted history
        body_with_history = "I changed my mind.\n\n> [1] ✅ Fix the fence"
        raw = _make_simple_email(from_addr="user@example.com", subject=f"UNDO:{task_hash}", body=body_with_history)
        sm = MagicMock(spec=SessionManager)
        
        should_reply, text, stats = process_email(raw, sm)
        
        assert should_reply is False
        assert mock_pipe.call_count == 1
        args = mock_pipe.call_args[0]
        assert "Mark the following task as NOT completed (undo): Fix the fence" in args[0]

    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_process_done_subject_hardwrapped_body(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)

        # Mail clients hard-wrap long plain-text lines (e.g. a long URL inside
        # a mailto body), splitting the task text across physical lines. The
        # hash won't match (no ☐/✅ prefix in a mailto-tap body), so this must
        # fall back to the whole body, not just its first line.
        body = (
            '<a href="\n'
            'https://example.com/post?xmt=abc&slof=1">Todd\n'
            'Mitchell on Threads</a> - A fun project.'
        )
        raw = _make_simple_email(from_addr="user@example.com", subject="DONE:deadbeef", body=body)
        sm = MagicMock(spec=SessionManager)

        should_reply, text, stats = process_email(raw, sm)

        assert should_reply is False
        args = mock_pipe.call_args[0]
        assert (
            'Mark the following task as completed: <a href=" '
            'https://example.com/post?xmt=abc&slof=1">Todd '
            'Mitchell on Threads</a> - A fun project.'
        ) in args[0]

    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_process_done_requires_reply_relays_message(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=True, output="Which task did you mean?", session_id="", stats=None
        )
        raw = _make_simple_email(from_addr="user@example.com", subject="DONE:deadbeef", body="Buy milk")
        sm = MagicMock(spec=SessionManager)

        should_reply, text, stats = process_email(raw, sm)

        assert should_reply is True
        assert text == "Which task did you mean?"


class TestSendReplyWithRetry:
    @patch("services.ingestion.channels.email.reply.send_reply")
    def test_succeeds_on_first_try(self, mock_send_reply):
        mock_send_reply.return_value = True

        result = _send_reply_with_retry(to_addr="a@b.com", subject="s", body="b")

        assert result is True
        assert mock_send_reply.call_count == 1

    @patch("services.ingestion.channels.email.reply.send_reply")
    def test_retries_once_then_succeeds(self, mock_send_reply):
        mock_send_reply.side_effect = [False, True]

        result = _send_reply_with_retry(to_addr="a@b.com", subject="s", body="b")

        assert result is True
        assert mock_send_reply.call_count == 2

    @patch("services.ingestion.channels.email.listener.logger")
    @patch("services.ingestion.channels.email.reply.send_reply")
    def test_logs_critical_when_both_attempts_fail(self, mock_send_reply, mock_logger):
        mock_send_reply.return_value = False

        result = _send_reply_with_retry(to_addr="a@b.com", subject="s", body="b")

        assert result is False
        assert mock_send_reply.call_count == 2
        mock_logger.critical.assert_called_once()


class TestEmailReplyLogic:
    @patch("services.ingestion.channels.email.reply.send_reply")
    @patch("services.ingestion.channels.email.listener.config")
    @patch("services.ingestion.channels.email.listener.process_email")
    def test_reply_to_override(self, mock_process, mock_config, mock_send_reply):
        # Config: Reply to admin, not sender
        mock_config.REPLY_TO_ADDRESS = "admin@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.RATE_LIMIT_MAX = 10
        mock_config.RATE_LIMIT_WINDOW_SECONDS = 60

        listener = EmailListener()
        # Mock dependencies
        listener.client = MagicMock()
        listener.rate_limiter = MagicMock()
        listener.rate_limiter.allow.return_value = True
        listener._archive_folder = "Archive"

        # Mock incoming email
        raw_bytes = _make_simple_email(from_addr="user@example.com", subject="Test")
        listener.client.fetch.return_value = {123: {b"RFC822": raw_bytes}}
        listener.client.search.return_value = []  # No more UNSEEN after processing

        # Mock process result (needs reply)
        mock_process.return_value = (True, "Error details", None)

        # Run
        listener._fetch_and_process([123])

        # Verify reply sent to admin
        mock_send_reply.assert_called_once()
        args = mock_send_reply.call_args[1]
        assert args["to_addr"] == "admin@example.com"
        assert args["body"] == "Error details"

    @patch("services.ingestion.channels.email.reply.send_reply")
    @patch("services.ingestion.channels.email.listener.config")
    @patch("services.ingestion.channels.email.listener.process_email")
    def test_reply_strict_to_reply_address(self, mock_process, mock_config, mock_send_reply):
        # Config: strict reply-to behavior
        mock_config.REPLY_TO_ADDRESS = "admin@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.RATE_LIMIT_MAX = 10
        mock_config.RATE_LIMIT_WINDOW_SECONDS = 60

        listener = EmailListener()
        listener.client = MagicMock()
        listener.rate_limiter = MagicMock()
        listener.rate_limiter.allow.return_value = True
        listener._archive_folder = "Archive"

        raw_bytes = _make_simple_email(from_addr="user@example.com", subject="Test")
        listener.client.fetch.return_value = {123: {b"RFC822": raw_bytes}}
        listener.client.search.return_value = []  # No more UNSEEN after processing

        mock_process.return_value = (True, "Details", None)

        listener._fetch_and_process([123])

        mock_send_reply.assert_called_once()
        args = mock_send_reply.call_args[1]
        assert args["to_addr"] == "admin@example.com"


def _make_reply_email(in_reply_to, body, from_addr="user@example.com", subject="Re: Synapse: E*TRADE re-authentication needed"):
    msg = MIMEText(body)
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["To"] = "inbox@lifeos.com"
    msg["In-Reply-To"] = in_reply_to
    return msg.as_bytes()


class TestProcessEmailEtradeAuth:
    @patch("services.ingestion.channels.email.listener.etrade_pin_auth")
    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_genuine_reply_completes_login(self, mock_config, mock_pipe, mock_etrade):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.ETRADE_CONSUMER_KEY = "key"
        mock_config.ETRADE_CONSUMER_SECRET = "secret"
        mock_config.ETRADE_MODE = "production"
        pending = {"channel": "email", "prompt_message_id": "<abc123@synapse>"}
        mock_etrade.load_pending.return_value = pending
        mock_etrade.finish_pin_auth.return_value = {"access_token": "AT", "access_token_secret": "AS"}

        raw = _make_reply_email("<abc123@synapse>", "654321")
        should_reply, text, stats = process_email(raw, MagicMock(spec=SessionManager))

        mock_etrade.finish_pin_auth.assert_called_once_with(pending, "654321", "key", "secret")
        mock_etrade.save_access_token.assert_called_once_with("AT", "AS", sandbox=False)
        mock_etrade.clear_pending.assert_called_once()
        mock_pipe.assert_not_called()
        assert should_reply is True
        assert "successful" in text.lower()

    @patch("services.ingestion.channels.email.listener.etrade_pin_auth")
    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_finish_failure_still_clears_pending(self, mock_config, mock_pipe, mock_etrade):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        pending = {"channel": "email", "prompt_message_id": "<abc123@synapse>"}
        mock_etrade.load_pending.return_value = pending
        mock_etrade.finish_pin_auth.side_effect = RuntimeError("invalid verifier")

        raw = _make_reply_email("<abc123@synapse>", "wrong-code")
        should_reply, text, stats = process_email(raw, MagicMock(spec=SessionManager))

        mock_etrade.clear_pending.assert_called_once()
        assert should_reply is True
        assert "failed" in text.lower()

    @patch("services.ingestion.channels.email.listener.etrade_pin_auth")
    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_reply_to_unrelated_message_is_not_intercepted(self, mock_config, mock_pipe, mock_etrade):
        """A reply to some other thread while a pending auth exists must
        not be swallowed as the code."""
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_etrade.load_pending.return_value = {"channel": "email", "prompt_message_id": "<abc123@synapse>"}
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)

        raw = _make_reply_email("<some-other-thread@gmail.com>", "hello there", subject="Re: Buy milk")
        process_email(raw, MagicMock(spec=SessionManager))

        mock_etrade.finish_pin_auth.assert_not_called()
        mock_pipe.assert_called_once()

    @patch("services.ingestion.channels.email.listener.etrade_pin_auth")
    @patch("services.ingestion.channels.email.listener.pipe_to_provider")
    @patch("services.ingestion.channels.email.listener.config")
    def test_no_pending_auth_falls_through_normally(self, mock_config, mock_pipe, mock_etrade):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_etrade.load_pending.return_value = None
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)

        raw = _make_simple_email(from_addr="user@example.com", body="Buy milk")
        process_email(raw, MagicMock(spec=SessionManager))

        mock_etrade.finish_pin_auth.assert_not_called()
        mock_pipe.assert_called_once()
