"""Tests for the email reply module."""

from unittest.mock import patch, MagicMock

from services.ingestion.email_reply import send_reply


class TestSendReply:
    @patch("services.ingestion.email_reply.config")
    def test_rejects_non_whitelisted(self, mock_config):
        mock_config.ALLOWED_SENDERS = ["allowed@example.com"]
        result = send_reply(
            to_addr="hacker@evil.com",
            subject="Test",
            body="Hello",
        )
        assert result is False

    @patch("services.ingestion.email_reply.smtplib.SMTP")
    @patch("services.ingestion.email_reply.config")
    def test_sends_to_whitelisted(self, mock_config, mock_smtp_class):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.EMAIL_ADDRESS = "bot@lifeos.com"
        mock_config.EMAIL_APP_PASSWORD = "secret"
        mock_config.SMTP_HOST = "smtp.gmail.com"
        mock_config.SMTP_PORT = 587

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        result = send_reply(
            to_addr="user@example.com",
            subject="Task Added",
            body="Your task was logged.",
        )
        assert result is True
        mock_server.send_message.assert_called_once()

    @patch("services.ingestion.email_reply.smtplib.SMTP")
    @patch("services.ingestion.email_reply.config")
    def test_adds_re_prefix(self, mock_config, mock_smtp_class):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.EMAIL_ADDRESS = "bot@lifeos.com"
        mock_config.EMAIL_APP_PASSWORD = "secret"
        mock_config.SMTP_HOST = "smtp.gmail.com"
        mock_config.SMTP_PORT = 587

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        send_reply(
            to_addr="user@example.com",
            subject="Buy groceries",
            body="Done.",
        )
        # Check the sent message has "Re:" prefix
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "Re: Buy groceries"

    @patch("services.ingestion.email_reply.smtplib.SMTP")
    @patch("services.ingestion.email_reply.config")
    def test_threading_headers(self, mock_config, mock_smtp_class):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.EMAIL_ADDRESS = "bot@lifeos.com"
        mock_config.EMAIL_APP_PASSWORD = "secret"
        mock_config.SMTP_HOST = "smtp.gmail.com"
        mock_config.SMTP_PORT = 587

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        send_reply(
            to_addr="user@example.com",
            subject="Test",
            body="Clarification needed.",
            original_message_id="<abc123@gmail.com>",
        )
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["In-Reply-To"] == "<abc123@gmail.com>"
        assert sent_msg["References"] == "<abc123@gmail.com>"
