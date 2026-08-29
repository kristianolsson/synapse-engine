"""
Tests for the standalone Telegram message sender.
"""

from unittest.mock import patch, AsyncMock, MagicMock


class TestSendTelegramMessage:
    """Tests for send_telegram_message (sync wrapper)."""

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_send_message_success(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_message = MagicMock()
        mock_message.message_id = 100
        mock_bot = AsyncMock()
        mock_bot.send_message.return_value = mock_message
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        result = send_telegram_message(12345, "Hello!")

        assert result == 100
        mock_bot.send_message.assert_called_once_with(chat_id=12345, text="Hello!", parse_mode='HTML', reply_markup=None)

    @patch("services.ingestion.channels.telegram.sender.config")
    def test_send_message_no_token(self, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = ""

        from services.ingestion.channels.telegram.sender import send_telegram_message

        result = send_telegram_message(12345, "Hello!")
        assert result is None

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_truncates_long_message(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_message = MagicMock()
        mock_message.message_id = 101
        mock_bot = AsyncMock()
        mock_bot.send_message.return_value = mock_message
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        long_text = "x" * 5000
        result = send_telegram_message(12345, long_text)

        assert result == 101
        sent_text = mock_bot.send_message.call_args[1]["text"]
        assert len(sent_text) <= 4096
        assert sent_text.endswith("...")

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_appends_stats_when_provided(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_message = MagicMock()
        mock_message.message_id = 102
        mock_bot = AsyncMock()
        mock_bot.send_message.return_value = mock_message
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        result = send_telegram_message(12345, "Balance: $1,000", stats={"duration_api_ms": 1200})

        assert result == 102
        sent_text = mock_bot.send_message.call_args[1]["text"]
        assert sent_text.startswith("Balance: $1,000")
        assert "[Stats: 1200ms]" in sent_text

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_no_stats_footer_when_stats_omitted(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_message = MagicMock()
        mock_message.message_id = 103
        mock_bot = AsyncMock()
        mock_bot.send_message.return_value = mock_message
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        send_telegram_message(12345, "Balance: $1,000")

        sent_text = mock_bot.send_message.call_args[1]["text"]
        assert sent_text == "Balance: $1,000"

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_send_message_api_error(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = Exception("API error")
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        result = send_telegram_message(12345, "Hello!")
        assert result is None

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_session_disabled_suppresses_stats(self, mock_telegram, mock_config):
        # Callers may pass a raw stats dict plus a session handle instead of
        # pre-gating themselves — the send function must do the gating.
        from services.ingestion.core.session_manager import UserSession

        mock_config.TELEGRAM_BOT_TOKEN = "test-token"
        mock_message = MagicMock()
        mock_message.message_id = 104
        mock_bot = AsyncMock()
        mock_bot.send_message.return_value = mock_message
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        mock_sm = MagicMock()
        mock_sm.get_stats_enabled.return_value = False
        session = UserSession(mock_sm, "12345")

        send_telegram_message(12345, "Balance: $1,000", stats={"duration_api_ms": 1200}, session=session)

        sent_text = mock_bot.send_message.call_args[1]["text"]
        assert sent_text == "Balance: $1,000"

    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_session_enabled_includes_stats(self, mock_telegram, mock_config):
        from services.ingestion.core.session_manager import UserSession

        mock_config.TELEGRAM_BOT_TOKEN = "test-token"
        mock_message = MagicMock()
        mock_message.message_id = 105
        mock_bot = AsyncMock()
        mock_bot.send_message.return_value = mock_message
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        mock_sm = MagicMock()
        mock_sm.get_stats_enabled.return_value = True
        session = UserSession(mock_sm, "12345")

        send_telegram_message(12345, "Balance: $1,000", stats={"duration_api_ms": 1200}, session=session)

        sent_text = mock_bot.send_message.call_args[1]["text"]
        assert "[Stats: 1200ms]" in sent_text
