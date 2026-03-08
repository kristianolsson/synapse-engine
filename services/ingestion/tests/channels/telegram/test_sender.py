"""
Tests for the standalone Telegram message sender.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


class TestSendTelegramMessage:
    """Tests for send_telegram_message (sync wrapper)."""

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_send_message_success(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_bot = AsyncMock()
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        result = send_telegram_message(12345, "Hello!")

        assert result is True
        mock_bot.send_message.assert_called_once_with(chat_id=12345, text="Hello!", parse_mode='HTML')

    @patch("services.ingestion.channels.telegram.sender.config")
    def test_send_message_no_token(self, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = ""

        from services.ingestion.channels.telegram.sender import send_telegram_message

        result = send_telegram_message(12345, "Hello!")
        assert result is False

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_truncates_long_message(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_bot = AsyncMock()
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        long_text = "x" * 5000
        result = send_telegram_message(12345, long_text)

        assert result is True
        sent_text = mock_bot.send_message.call_args[1]["text"]
        assert len(sent_text) <= 4096
        assert sent_text.endswith("...")

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_send_message_api_error(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = Exception("API error")
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        result = send_telegram_message(12345, "Hello!")
        assert result is False
