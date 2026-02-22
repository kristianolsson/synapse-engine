"""Tests for the email listener module."""

import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from unittest.mock import patch, MagicMock

import tempfile

from services.ingestion.email_listener import (
    EmailListener,
    extract_sender,
    extract_text_body,
    extract_images,
    process_email,
)
from services.ingestion.session_manager import SessionManager
from services.ingestion.rate_limiter import RateLimiter


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

    @patch("services.ingestion.rate_limiter.time")
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


class TestExtractImages:
    def test_no_images(self):
        raw = _make_simple_email()
        msg = email.message_from_bytes(raw)
        images = extract_images(msg)
        assert images == []

    @patch("services.ingestion.email_listener.config")
    def test_with_image_attachment(self, mock_config, tmp_path):
        mock_config.VAULT_PATH = str(tmp_path)
        msg = MIMEMultipart()
        msg["From"] = "test@example.com"
        msg.attach(MIMEText("See attached", "plain"))
        # Create a fake image
        img = MIMEImage(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, name="test.png")
        msg.attach(img)
        parsed = email.message_from_bytes(msg.as_bytes())
        images = extract_images(parsed)
        assert len(images) == 1
        assert "test.png" in images[0]


# ── process_email tests ─────────────────────────────────────────────


class TestProcessEmail:
    @patch("services.ingestion.email_listener.pipe_to_gemini")
    @patch("services.ingestion.email_listener.config")
    def test_rejects_unauthorized_sender(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["allowed@example.com"]
        raw = _make_simple_email(from_addr="hacker@evil.com")
        sm = MagicMock(spec=SessionManager)
        should_reply, text, stats = process_email(raw, sm)
        assert should_reply is False
        assert stats is None
        mock_pipe.assert_not_called()

    @patch("services.ingestion.email_listener.pipe_to_gemini")
    @patch("services.ingestion.email_listener.config")
    def test_success_silent(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)
        raw = _make_simple_email(from_addr="user@example.com", body="Buy milk")
        sm = MagicMock(spec=SessionManager)
        should_reply, text, stats = process_email(raw, sm)
        assert should_reply is False
        assert text == ""
        assert stats is None

    @patch("services.ingestion.email_listener.pipe_to_gemini")
    @patch("services.ingestion.email_listener.config")
    def test_error_triggers_reply(self, mock_config, mock_pipe):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=True, output="Repo is locked", session_id="", stats={"models": {}})
        raw = _make_simple_email(from_addr="user@example.com", body="Add todo")
        sm = MagicMock(spec=SessionManager)
        should_reply, text, stats = process_email(raw, sm)
        assert should_reply is True
        assert "Repo is locked" in text
        assert stats is not None


class TestEmailReplyLogic:
    @patch("services.ingestion.email_reply.send_reply")
    @patch("services.ingestion.email_listener.config")
    @patch("services.ingestion.email_listener.process_email")
    def test_reply_to_override(self, mock_process, mock_config, mock_send_reply):
        # Config: Reply to admin, not sender
        mock_config.REPLY_TO_ADDRESS = "admin@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.RATE_LIMIT_MAX = 10
        mock_config.RATE_LIMIT_WINDOW_SECONDS = 60

        from services.ingestion.email_listener import EmailListener
        listener = EmailListener()
        # Mock dependencies
        listener.client = MagicMock()
        listener.rate_limiter = MagicMock()
        listener.rate_limiter.allow.return_value = True

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

    @patch("services.ingestion.email_reply.send_reply")
    @patch("services.ingestion.email_listener.config")
    @patch("services.ingestion.email_listener.process_email")
    def test_reply_to_sender_default(self, mock_process, mock_config, mock_send_reply):
        # Config: No override
        mock_config.REPLY_TO_ADDRESS = ""
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.RATE_LIMIT_MAX = 10
        mock_config.RATE_LIMIT_WINDOW_SECONDS = 60

        from services.ingestion.email_listener import EmailListener
        listener = EmailListener()
        listener.client = MagicMock()
        listener.rate_limiter = MagicMock()
        listener.rate_limiter.allow.return_value = True

        raw_bytes = _make_simple_email(from_addr="user@example.com", subject="Test")
        listener.client.fetch.return_value = {123: {b"RFC822": raw_bytes}}
        listener.client.search.return_value = []  # No more UNSEEN after processing

        mock_process.return_value = (True, "Details", None)

        listener._fetch_and_process([123])

        mock_send_reply.assert_called_once()
        args = mock_send_reply.call_args[1]
        assert args["to_addr"] == "user@example.com"
