"""Tests for the email reply module."""

from unittest.mock import patch, MagicMock

from services.ingestion.services.email.reply import send_reply


class TestSendReply:
    @patch("services.ingestion.services.email.reply.config")
    def test_rejects_non_whitelisted(self, mock_config):
        mock_config.ALLOWED_SENDERS = ["allowed@example.com"]
        result = send_reply(
            to_addr="hacker@evil.com",
            subject="Test",
            body="Hello",
        )
        assert result is False

    @patch("services.ingestion.services.email.reply.smtplib.SMTP")
    @patch("services.ingestion.services.email.reply.config")
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

    @patch("services.ingestion.services.email.reply.smtplib.SMTP")
    @patch("services.ingestion.services.email.reply.config")
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
            original_message_id="<123@test.com>",
        )
        # Check the sent message has "Re:" prefix
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "Re: Buy groceries"

    @patch("services.ingestion.services.email.reply.smtplib.SMTP")
    @patch("services.ingestion.services.email.reply.config")
    def test_stats_formatting_with_errors(self, mock_config, mock_smtp_class):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        stats = {
            "models": {
                "gemini-fail": {
                    "api": {"totalLatencyMs": 500, "totalErrors": 2, "totalRequests": 3},
                }
            }
        }

        send_reply(
            to_addr="user@example.com",
            subject="Bug report",
            body="Failed.",
            stats=stats
        )

        sent_msg = mock_server.send_message.call_args[0][0]
        body_content = sent_msg.get_payload(decode=True).decode("utf-8")
        assert "gemini-fail: 3 requests, 2 errors, 500ms" in body_content
        assert "<b>Stats:</b>" in body_content

    @patch("services.ingestion.services.email.reply.smtplib.SMTP")
    @patch("services.ingestion.services.email.reply.config")
    def test_renders_form_fields_as_table(self, mock_config, mock_smtp_class):
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        mock_config.EMAIL_ADDRESS = "bot@lifeos.com"
        mock_config.EMAIL_APP_PASSWORD = "secret"
        mock_config.SMTP_HOST = "smtp.gmail.com"
        mock_config.SMTP_PORT = 587

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        body = (
            "Tonight's check-in:\n"
            "☐F:yn:protein Protein at every meal?\n"
            "☐F:text:sleep Sleep (bedtime / hrs)\n"
        )
        send_reply(to_addr="user@example.com", subject="Check-in", body=body)

        sent_msg = mock_server.send_message.call_args[0][0]
        body_content = sent_msg.get_payload(decode=True).decode("utf-8")
        assert "☐F:" not in body_content
        assert "<table" in body_content
        assert "Protein at every meal?" in body_content
        assert "Sleep (bedtime / hrs)" in body_content
        assert "Yes / No" in body_content

    @patch("services.ingestion.services.email.reply.smtplib.SMTP")
    @patch("services.ingestion.services.email.reply.config")
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
        send_reply(
            to_addr="user@example.com",
            subject="Test",
            body="Clarification needed.",
            original_message_id="<abc123@gmail.com>",
            original_references="<root-id@gmail.com> <prev-id@gmail.com>",
        )
        sent_msg_with_refs = mock_server.send_message.call_args[0][0]
        assert sent_msg_with_refs["In-Reply-To"] == "<abc123@gmail.com>"
        assert sent_msg_with_refs["References"] == "<root-id@gmail.com> <prev-id@gmail.com> <abc123@gmail.com>"

    @patch("services.ingestion.services.email.reply.smtplib.SMTP")
    @patch("services.ingestion.services.email.reply.config")
    def test_sends_html_with_br_newlines(self, mock_config, mock_smtp_class):
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
            body="Line one\nLine two\nLine three",
        )
        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg.get_content_type() == "text/html"
        body_content = sent_msg.get_payload(decode=True).decode("utf-8")
        assert "Line one<br>Line two<br>Line three" in body_content
