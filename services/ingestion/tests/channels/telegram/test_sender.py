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
    def test_splits_long_message(self, mock_telegram, mock_config):
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        mock_message = MagicMock()
        mock_message.message_id = 101
        mock_bot = AsyncMock()
        mock_bot.send_message.return_value = mock_message
        mock_telegram.Bot.return_value = mock_bot

        from services.ingestion.channels.telegram.sender import send_telegram_message

        long_text = "x" * 5000
        result = send_telegram_message(12345, long_text)

        # Split across messages instead of truncated; returns the last id.
        assert result == 101
        sent = [c[1]["text"] for c in mock_bot.send_message.call_args_list]
        assert len(sent) == 2
        assert all(len(t) <= 4096 for t in sent)
        assert "".join(sent) == long_text

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


class TestSplitTelegramHtml:
    def test_short_text_unchanged(self):
        from services.ingestion.channels.telegram.sender import split_telegram_html
        assert split_telegram_html("<b>hi</b>") == ["<b>hi</b>"]

    def test_splits_on_lines_and_rebalances_tags(self):
        from services.ingestion.channels.telegram.sender import split_telegram_html
        lines = [f'<a href="https://example.com/{i}">item {i} &amp; more</a>' for i in range(300)]
        text = "<blockquote>" + "\n".join(lines) + "</blockquote>"
        chunks = split_telegram_html(text)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 4096
            assert c.startswith("<blockquote>") and c.endswith("</blockquote>")
            assert c.count("<a ") == c.count("</a>")

    def test_hard_split_never_cuts_inside_a_tag(self):
        from services.ingestion.channels.telegram.sender import split_telegram_html
        text = "<b>" + ("word " * 2000) + "</b>"
        chunks = split_telegram_html(text)
        assert len(chunks) > 1
        for c in chunks:
            assert c.startswith("<b>") and c.endswith("</b>")


class TestPlainTextFallback:
    @patch("services.ingestion.channels.telegram.sender.config")
    @patch("services.ingestion.channels.telegram.sender.telegram")
    def test_resends_as_plain_text_on_parse_error(self, mock_telegram, mock_config):
        from telegram.error import BadRequest
        from services.ingestion.channels.telegram.sender import send_telegram_message

        mock_config.TELEGRAM_BOT_TOKEN = "test-token"
        ok = MagicMock()
        ok.message_id = 7
        mock_bot = AsyncMock()
        mock_bot.send_message.side_effect = [BadRequest("Can't parse entities: unclosed tag"), ok]
        mock_telegram.Bot.return_value = mock_bot

        result = send_telegram_message(12345, "<b>Bold &amp; broken")

        assert result == 7
        retry_kwargs = mock_bot.send_message.call_args_list[1][1]
        assert retry_kwargs["text"] == "Bold & broken"
        assert "parse_mode" not in retry_kwargs
