"""Tests for the Telegram listener module."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from services.ingestion import config
from services.ingestion.core.rate_limiter import RateLimiter
from services.ingestion.core.session_manager import SessionManager
from services.ingestion.channels.telegram import listener
from services.ingestion.channels.telegram.listener import (
    handle_message,
    handle_callback_query,
    download_attachment,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _make_update(user_id=12345, chat_type="private", text="Buy milk", photo=None, document=None):
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.message.from_user.id = user_id
    update.message.chat.type = chat_type
    update.message.chat.id = user_id
    update.message.text = text
    update.message.caption = None
    update.message.photo = photo or []
    update.message.document = document
    update.message.voice = None
    update.message.reply_text = AsyncMock()
    update.get_bot.return_value = MagicMock()
    return update


# ── Security tests ──────────────────────────────────────────────────


class TestTelegramSecurity:
    def setUp(self):
        config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        self.rate_limiter = RateLimiter(10, 60)
        self.session_manager = MagicMock(spec=SessionManager)
        self.session_manager.get_session.return_value = None

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_rejects_group_chat(self, mock_pipe, mock_config):
        self.setUp()
        update = _make_update(chat_type="group")

        await handle_message(update, None, self.rate_limiter, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_rejects_unauthorized_user(self, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [99999]
        rl = RateLimiter(10, 60)
        update = _make_update(user_id=12345)

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_accepts_authorized_user(self, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="")
        rl = RateLimiter(10, 60)
        update = _make_update(user_id=12345, text="Add todo")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_called_once()


# ── Rate Limiting tests ────────────────────────────────────────────


class TestTelegramRateLimiting:
    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_rate_limit_blocks_message(self, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        rl = RateLimiter(1, 60)
        rl.allow()  # Exhaust the limiter
        update = _make_update()

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_called_once()
        assert "Rate limit" in update.message.reply_text.call_args[0][0]


# ── Message Processing tests ───────────────────────────────────────


class TestTelegramMessageProcessing:
    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_replies_checkmark(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)
        rl = RateLimiter(10, 60)
        update = _make_update(text="Buy groceries")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        update.message.reply_text.assert_called_once_with("✓", parse_mode='HTML', reply_markup=None)

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_with_stats(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []

        stats = {
            "models": {
                "gemini-pro": {"api": {"totalRequests": 2, "totalErrors": 0, "totalLatencyMs": 100}}
            }
        }
        mock_pipe.return_value = MagicMock(
            is_error=False,
            requires_reply=False,
            output="Done",
            session_id="",
            stats=stats
        )
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = True
        update = _make_update(text="Analyze this")

        await handle_message(update, None, rl, sm)

        args = update.message.reply_text.call_args[0][0]
        assert "Done" in args
        assert "gemini-pro (2 req, 0 err)" in args

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_with_stats_disabled(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []

        stats = {
            "models": {
                "gemini-pro": {"api": {"totalRequests": 2, "totalErrors": 0, "totalLatencyMs": 100}}
            }
        }
        mock_pipe.return_value = MagicMock(
            is_error=False,
            requires_reply=False,
            output="Done",
            session_id="",
            stats=stats
        )
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = False
        update = _make_update(text="Analyze this")

        await handle_message(update, None, rl, sm)

        args = update.message.reply_text.call_args[0][0]
        assert "Done" in args
        assert "Stats" not in args

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_passes_correlation_extra_env(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="sess-new", stats=None)
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        update = _make_update(text="Buy milk", user_id=12345)
        update.message.reply_to_message = None

        await handle_message(update, None, rl, sm)

        extra_env = mock_pipe.call_args.kwargs["extra_env"]
        assert extra_env["SYNAPSE_SESSION_KEY"] == "12345"
        assert extra_env["SYNAPSE_SESSION_ID"] == ""
        assert extra_env["SYNAPSE_CHANNEL"] == "telegram"
        assert extra_env["SYNAPSE_CHAT_ID"] == "12345"

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_backfills_session_id(self, mock_extract, mock_pipe, mock_config, mock_etrade):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="sess-new", stats=None)
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        update = _make_update(text="Buy milk", user_id=12345)
        update.message.reply_to_message = None

        await handle_message(update, None, rl, sm)

        mock_etrade.backfill_session_id.assert_called_once_with("12345", "sess-new")

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_with_tool_stats(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []

        stats = {
            "models": {
                "gemini-pro": {"api": {"totalRequests": 1, "totalErrors": 0}}
            },
            "tools": {
                "byName": {
                    "google_web_search": {"count": 2, "success": 1, "fail": 1}
                }
            }
        }
        mock_pipe.return_value = MagicMock(
            is_error=False,
            requires_reply=False,
            output="Done",
            session_id="",
            stats=stats
        )
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = True
        update = _make_update(text="Analyze this")

        await handle_message(update, None, rl, sm)

        args = update.message.reply_text.call_args[0][0]
        assert "Done" in args
        assert "gemini-pro (1 req, 0 err)" in args
        assert "google_web_search: 2 (1 ok, 1 fail)" in args

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_error_replies_with_output(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=True, output="Repo is locked", session_id="", stats=None)
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = False
        update = _make_update(text="Add todo")

        await handle_message(update, None, rl, sm)

        update.message.reply_text.assert_called_once_with("Repo is locked", parse_mode='HTML', reply_markup=None)

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_empty_message_ignored(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        rl = RateLimiter(10, 60)
        update = _make_update(text="")
        update.message.text = ""
        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_voice_message_replies_unsupported(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        rl = RateLimiter(10, 60)

        update = _make_update(text="")
        update.message.voice = MagicMock()

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_called_once_with("Sorry, voice notes are not supported yet.")

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_prompt_contains_telegram_type(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="")
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = False
        update = _make_update(text="Hello")

        await handle_message(update, None, rl, sm)

        prompt_arg = mock_pipe.call_args[0][0]
        assert "Type: telegram" in prompt_arg
        assert "Sender: 12345" in prompt_arg

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    async def test_stats_on_command(self, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        update = _make_update(text="/stats on")

        await handle_message(update, None, rl, sm)

        sm.set_stats_enabled.assert_called_once_with("12345", True)
        update.message.reply_text.assert_called_once_with("Stats display turned on.")

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    async def test_stats_off_command(self, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        update = _make_update(text="/stats off")

        await handle_message(update, None, rl, sm)

        sm.set_stats_enabled.assert_called_once_with("12345", False)
        update.message.reply_text.assert_called_once_with("Stats display turned off.")


# ── /help command tests ─────────────────────────────────────────────
# A prior bug used **double asterisks** plus a stray asterisk in "E*TRADE",
# leaving an odd number of '*' in legacy Telegram Markdown. Telegram then
# rejected the whole message with a 400 "can't parse entities", which had
# no try/except and so vanished silently. These tests pin the fixed text
# and prove a future formatting slip degrades to plain text instead.


class TestHelpCommand:
    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    async def test_help_text_has_balanced_asterisks(self, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        rl = RateLimiter(10, 60)
        update = _make_update(text="/help")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        update.message.reply_text.assert_called_once()
        help_text = update.message.reply_text.call_args[0][0]
        assert help_text.count("*") % 2 == 0
        assert "E*TRADE" not in help_text
        assert update.message.reply_text.call_args[1]["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    async def test_help_falls_back_to_plain_text_if_markdown_ever_breaks_again(self, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        rl = RateLimiter(10, 60)
        update = _make_update(text="/help")
        update.message.reply_text.side_effect = [
            BadRequest("Can't parse entities: can't find end of the entity"),
            "sent",
        ]

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        assert update.message.reply_text.await_count == 2


# ── Quota/rate-limit message tests ──────────────────────────────────
# The "hit a limit" notice interpolates the AI provider's raw error text
# straight into an HTML-parsed message. That text is untrusted (provider
# stack traces, JSON, etc. can contain '<' or '&') so it must be sanitized,
# and the send itself must degrade to plain text rather than vanish.


class TestQuotaLimitMessage:
    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_provider_error_text_is_sanitized_before_sending(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.get_ai_provider.return_value = "gemini"
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(
            is_error=True,
            provider_name="gemini",
            output="resource_exhausted: <trace>boom</trace> a < b",
            session_id="",
            stats=None,
        )
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        update = _make_update(text="Do a thing")

        await handle_message(update, None, rl, sm)

        update.message.reply_text.assert_called_once()
        sent_text = update.message.reply_text.call_args[0][0]
        assert "<trace>" not in sent_text
        assert update.message.reply_text.call_args[1]["parse_mode"] == "HTML"

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_falls_back_to_plain_text_if_still_unparseable(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.get_ai_provider.return_value = "gemini"
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(
            is_error=True,
            provider_name="gemini",
            output="429 quota exceeded",
            session_id="",
            stats=None,
        )
        rl = RateLimiter(10, 60)
        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        update = _make_update(text="Do a thing")
        update.message.reply_text.side_effect = [
            BadRequest("Can't parse entities: unsupported start tag"),
            MagicMock(message_id=1),
        ]

        await handle_message(update, None, rl, sm)

        assert update.message.reply_text.await_count == 2
        second_call_kwargs = update.message.reply_text.call_args_list[1][1]
        assert "parse_mode" not in second_call_kwargs


# ── Attachment tests ────────────────────────────────────────────────


class TestTelegramAttachments:
    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.MAX_FILE_BYTES", 1 * 1024 * 1024)
    @patch("services.ingestion.channels.telegram.listener.config")
    async def test_download_skips_large_file(self, mock_config, tmp_path):
        mock_config.VAULT_PATH = str(tmp_path)
        mock_config.TELEGRAM_MAX_FILE_SIZE_MB = 1

        file_obj = MagicMock()
        file_obj.file_size = 2 * 1024 * 1024  # 2MB > 1MB cap
        file_obj.file_id = "abc123"

        bot = AsyncMock()
        result = await download_attachment(file_obj, "big_file.zip", bot)

        assert result is None
        bot.get_file.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    async def test_download_saves_file(self, mock_config, tmp_path):
        mock_config.VAULT_PATH = str(tmp_path)
        mock_config.TELEGRAM_MAX_FILE_SIZE_MB = 10

        file_obj = MagicMock()
        file_obj.file_size = 1024
        file_obj.file_id = "abc123"
        file_obj.file_unique_id = "unique123"

        tg_file = AsyncMock()
        bot = AsyncMock()
        bot.get_file.return_value = tg_file

        result = await download_attachment(file_obj, "photo.jpg", bot)

        assert result is not None
        assert "photo.jpg" in result
        tg_file.download_to_drive.assert_called_once()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_reply_to_bot_message_resumes_session(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        
        # Setup pipe to return a new session ID
        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=False, output="Context output", session_id="new-session", stats=None
        )
        
        # Setup session manager to have a stored message -> session mapping
        sm = MagicMock(spec=SessionManager)
        sm.get_message_session.side_effect = lambda msg_id: "linked-session" if msg_id == 999 else None
        sm.get_session.return_value = "default-session"
        
        rl = RateLimiter(10, 60)
        
        # User replies to a specific bot message (ID 999)
        update = _make_update(text="Following up on that")
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.message_id = 999
        update.message.reply_to_message.text = "Original reminder"
        
        mock_sent_msg = MagicMock()
        mock_sent_msg.message_id = 1000
        update.message.reply_text.return_value = mock_sent_msg
        
        from services.ingestion.channels.telegram.listener import handle_message
        await handle_message(update, None, rl, sm)
        
        # Verify the session manager linked the session properly
        sm.get_message_session.assert_called_once_with(999)
        sm.get_session.assert_not_called()  # Did not fall back to default
        
        # Verify pipe_to_provider was called with the linked session and injected context
        args, kwargs = mock_pipe.call_args
        actual_prompt = args[0]
        actual_session_id = kwargs.get("session_id") if "session_id" in kwargs else args[1]
        
        assert actual_session_id == "linked-session"
        assert "Context: You previously sent the user this message: \"Original reminder\"" in actual_prompt
        assert "The user replied to that message with: \"Following up on that\"" in actual_prompt
        
        # Verify the NEW message ID (1000) was saved to the NEW session ID returned by pipe
        sm.save_message_session.assert_called_once_with(1000, "new-session")


@pytest.mark.asyncio
@patch("services.ingestion.channels.telegram.listener.config")
class TestTaskButtons:
    """Tests for inline keyboard attachment and callback query handling."""

    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments")
    async def test_response_with_tasks_gets_keyboard(self, mock_extract, mock_pipe, mock_config):
        """When Gemini returns ☐ items, reply should include inline buttons."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.TELEGRAM_MAX_FILE_SIZE_MB = 10
        mock_extract.return_value = []

        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=False,
            output="Your tasks:\n☐ Buy groceries\n☐ Fix the fence",
            session_id="test-session", stats=None
        )

        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = False
        rl = RateLimiter(10, 60)

        update = _make_update(text="what are my todos?")
        update.message.reply_to_message = None
        mock_sent = MagicMock()
        mock_sent.message_id = 500
        update.message.reply_text.return_value = mock_sent

        await handle_message(update, None, rl, sm)

        # Verify reply_text was called with a reply_markup
        call_kwargs = update.message.reply_text.call_args[1]
        assert call_kwargs.get("reply_markup") is not None
        keyboard = call_kwargs["reply_markup"]
        assert len(keyboard.inline_keyboard) == 1
        assert len(keyboard.inline_keyboard[0]) == 2
        assert keyboard.inline_keyboard[0][0].callback_data.startswith("done_")

    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments")
    async def test_response_without_tasks_gets_no_keyboard(self, mock_extract, mock_pipe, mock_config):
        """When Gemini returns no ☐ items, reply should have no keyboard."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.TELEGRAM_MAX_FILE_SIZE_MB = 10
        mock_extract.return_value = []

        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=False,
            output="All tasks are done!",
            session_id="test-session", stats=None
        )

        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = False
        rl = RateLimiter(10, 60)

        update = _make_update(text="what are my todos?")
        update.message.reply_to_message = None
        mock_sent = MagicMock()
        mock_sent.message_id = 501
        update.message.reply_text.return_value = mock_sent

        await handle_message(update, None, rl, sm)

        call_kwargs = update.message.reply_text.call_args[1]
        assert call_kwargs.get("reply_markup") is None

    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_callback_query_completes_task(self, mock_pipe, mock_config):
        """Tapping a ✅ button pipes completion to Gemini and removes the button."""
        from services.ingestion.utils.task_formatter import _hash_task

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        task_hash = _hash_task("Buy groceries")
        mock_pipe.return_value = MagicMock(
            is_error=False, output="Done!", session_id="completion-session", stats=None
        )

        sm = MagicMock(spec=SessionManager)
        sm.get_message_session.return_value = "existing-session"
        sm.get_session.return_value = "fallback-session"

        update = MagicMock()
        update.callback_query.data = f"done_{task_hash}"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.message_id = 999
        update.callback_query.message.text = "Your tasks:\n☐ Buy groceries\n☐ Fix the fence"

        # Mock inline keyboard with two buttons
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        update.callback_query.message.reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Buy groceries", callback_data=f"done_{task_hash}")],
            [InlineKeyboardButton("✅ Fix the fence", callback_data=f"done_{_hash_task('Fix the fence')}")],
        ])
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.edit_text = AsyncMock()
        update.callback_query.message.reply_text = AsyncMock()

        context = MagicMock()
        context.background_tasks = set()
        await handle_callback_query(update, context, sm)
        if context.background_tasks:
            await asyncio.gather(*context.background_tasks)
        # Verify pipe was called with completion prompt
        args, _ = mock_pipe.call_args
        assert "Mark the following task as completed: Buy groceries" in args[0]

        # Verify edit_text was called to update the message
        update.callback_query.message.edit_text.assert_called_once()
        edit_args, edit_kwargs = update.callback_query.message.edit_text.call_args
        edited_text = edit_args[0]
        assert "✅ Buy groceries" in edited_text
        
        # Verify the button state was toggled completely
        assert edit_kwargs["reply_markup"] is not None
        new_kb = edit_kwargs["reply_markup"].inline_keyboard
        assert len(new_kb) == 2
        # First button (clicked) changed to undo
        assert new_kb[0][0].text == "↩️ Buy groceries"
        assert new_kb[0][0].callback_data == f"undo_{task_hash}"
        # Second button remained the same
        assert new_kb[1][0].text == "✅ Fix the fence"
        assert new_kb[1][0].callback_data == f"done_{_hash_task('Fix the fence')}"

    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_callback_query_undo_task(self, mock_pipe, mock_config):
        from services.ingestion.utils.task_formatter import _hash_task
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        task_hash = _hash_task("Buy groceries")
        mock_pipe.return_value = MagicMock(
            is_error=False, output="Done!", session_id="completion-session", stats=None
        )

        sm = MagicMock(spec=SessionManager)
        sm.get_message_session.return_value = "existing-session"

        update = MagicMock()
        update.callback_query.data = f"undo_{task_hash}"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.message_id = 999
        # Message text was previously marked complete
        update.callback_query.message.text = "Your tasks:\n✅ Buy groceries\n☐ Fix the fence"

        # Mock inline keyboard with two buttons
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        update.callback_query.message.reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Buy groceries", callback_data=f"undo_{task_hash}")],
            [InlineKeyboardButton("✅ Fix the fence", callback_data=f"done_{_hash_task('Fix the fence')}")],
        ])
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.edit_text = AsyncMock()

        context = MagicMock()
        context.background_tasks = set()
        await handle_callback_query(update, context, sm)
        if context.background_tasks:
            await asyncio.gather(*context.background_tasks)
        # Verify pipe was called with undo prompt
        args, _ = mock_pipe.call_args
        assert "Mark the following task as NOT completed (undo): Buy groceries" in args[0]

        # Verify edited text reverted from ✅ to ☐
        edit_args, edit_kwargs = update.callback_query.message.edit_text.call_args
        edited_text = edit_args[0]
        assert "☐ Buy groceries" in edited_text

        # Verify button reverted state
        new_kb = edit_kwargs["reply_markup"].inline_keyboard
        assert new_kb[0][0].text == "✅ Buy groceries"
        assert new_kb[0][0].callback_data == f"done_{task_hash}"

    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_callback_query_rollback_on_error(self, mock_pipe, mock_config):
        """If Gemini fails, the optimistic UI update should be rolled back."""
        from services.ingestion.utils.task_formatter import _hash_task
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        task_hash = _hash_task("Buy groceries")
        mock_pipe.return_value = MagicMock(
            is_error=True, output="Network Error", session_id="err-session", stats=None
        )

        sm = MagicMock(spec=SessionManager)

        update = MagicMock()
        update.callback_query.data = f"done_{task_hash}"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.message_id = 999
        update.callback_query.message.text = "Your tasks:\n☐ Buy groceries"

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        original_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Buy groceries", callback_data=f"done_{task_hash}")],
        ])
        update.callback_query.message.reply_markup = original_markup
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.edit_text = AsyncMock()
        update.callback_query.message.reply_text = AsyncMock()

        context = MagicMock()
        context.background_tasks = set()
        await handle_callback_query(update, context, sm)
        if context.background_tasks:
            await asyncio.gather(*context.background_tasks)
        # Should be edited twice: once for optimistic update, once for rollback
        assert update.callback_query.message.edit_text.call_count == 2
        
        # First edit: Optimistic check
        first_args, first_kwargs = update.callback_query.message.edit_text.call_args_list[0]
        assert "✅ Buy groceries" in first_args[0]
        assert first_kwargs["reply_markup"].inline_keyboard[0][0].text == "↩️ Buy groceries"
        
        # Second edit: Rollback to original
        second_args, second_kwargs = update.callback_query.message.edit_text.call_args_list[1]
        assert "☐ Buy groceries" in second_args[0]
        # Restored the original markup
        assert second_kwargs["reply_markup"] == original_markup
        
        # Should also notify the user
        update.callback_query.message.reply_text.assert_called_once()
        assert "Rolling back" in update.callback_query.message.reply_text.call_args[0][0]

    async def test_callback_query_unknown_hash(self, mock_config):
        """When hash can't be matched, show error toast and don't pipe to Gemini."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        sm = MagicMock(spec=SessionManager)

        update = MagicMock()
        update.callback_query.data = "done_deadbeef"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.text = "No tasks here"
        update.callback_query.answer = AsyncMock()

        await handle_callback_query(update, None, sm)

        # Should show error toast
        update.callback_query.answer.assert_called_once()
        call_args = update.callback_query.answer.call_args
        assert "Could not identify" in call_args[0][0]

    async def test_callback_query_unauthorized_user(self, mock_config):
        """Unauthorized users should be rejected."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [99999]

        sm = MagicMock(spec=SessionManager)

        update = MagicMock()
        update.callback_query.data = "done_abc12345"
        update.callback_query.from_user.id = 12345
        update.callback_query.answer = AsyncMock()

        await handle_callback_query(update, None, sm)

        update.callback_query.answer.assert_called_once()
        call_args = update.callback_query.answer.call_args
        assert "Unauthorized" in call_args[0][0]


# ── Claude re-auth tests ────────────────────────────────────────────


class TestTelegramClaudeAuth:
    def setup_method(self):
        listener._pending_claude_auth.clear()

    def teardown_method(self):
        listener._pending_claude_auth.clear()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener._start_claude_auth")
    async def test_update_claude_auth_replies_with_url(self, mock_start, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_start.return_value = (MagicMock(), "https://claude.ai/oauth/authorize?code=abc")
        rl = RateLimiter(10, 60)
        update = _make_update(text="/update-claude-auth")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        assert "12345" in listener._pending_claude_auth
        reply = update.message.reply_text.call_args[0][0]
        assert "https://claude.ai/oauth/authorize?code=abc" in reply

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener._start_claude_auth")
    async def test_update_claude_auth_start_failure(self, mock_start, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_start.side_effect = RuntimeError("boom")
        rl = RateLimiter(10, 60)
        update = _make_update(text="/update-claude-auth")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        assert "12345" not in listener._pending_claude_auth
        reply = update.message.reply_text.call_args[0][0]
        assert "Failed to start Claude login" in reply

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener._finish_claude_auth")
    async def test_pasted_code_completes_login(self, mock_finish, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_finish.return_value = (True, "Login successful")
        listener._pending_claude_auth["12345"] = {
            "child": MagicMock(),
            "created_at": time.time(),
        }
        rl = RateLimiter(10, 60)
        update = _make_update(text="123456")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_finish.assert_called_once()
        assert "12345" not in listener._pending_claude_auth
        replies = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("successful" in r.lower() for r in replies)

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener._finish_claude_auth")
    async def test_pasted_code_reports_failure(self, mock_finish, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_finish.return_value = (False, "invalid code")
        listener._pending_claude_auth["12345"] = {
            "child": MagicMock(),
            "created_at": time.time(),
        }
        rl = RateLimiter(10, 60)
        update = _make_update(text="wrong-code")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        assert "12345" not in listener._pending_claude_auth
        replies = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("failed" in r.lower() for r in replies)

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener._finish_claude_auth")
    async def test_expired_pending_auth_is_rejected(self, mock_finish, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_child = MagicMock()
        listener._pending_claude_auth["12345"] = {
            "child": mock_child,
            "created_at": time.time() - listener.CLAUDE_AUTH_TIMEOUT_SECONDS - 1,
        }
        rl = RateLimiter(10, 60)
        update = _make_update(text="123456")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_finish.assert_not_called()
        mock_child.close.assert_called_once_with(force=True)
        assert "12345" not in listener._pending_claude_auth
        reply = update.message.reply_text.call_args[0][0]
        assert "expired" in reply.lower()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_pending_auth_ignored_for_slash_commands(self, mock_pipe, mock_config):
        """A slash command while a login is pending should not be swallowed as the code."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        listener._pending_claude_auth["12345"] = {
            "child": MagicMock(),
            "created_at": time.time(),
        }
        rl = RateLimiter(10, 60)
        update = _make_update(text="/stats on")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        # Pending auth should be untouched, and /stats should have run normally.
        assert "12345" in listener._pending_claude_auth
        update.message.reply_text.assert_called_once_with("Stats display turned on.")


# ── E*TRADE re-auth tests ───────────────────────────────────────────


class TestTelegramEtradeAuth:
    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    async def test_update_etrade_auth_replies_with_url_and_marks_prompt_sent(self, mock_etrade, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.ETRADE_CONSUMER_KEY = "key"
        mock_config.ETRADE_CONSUMER_SECRET = "secret"
        mock_etrade.start_pin_auth.return_value = {"authorize_url": "https://us.etrade.com/authorize?token=abc"}
        mock_etrade.PENDING_TTL_SECONDS = 1800
        rl = RateLimiter(10, 60)
        update = _make_update(text="/update-etrade-auth")
        update.message.reply_text.return_value = MagicMock(message_id=555)

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_etrade.clear_pending.assert_called_once()
        mock_etrade.start_pin_auth.assert_called_once_with("key", "secret")
        replies = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("https://us.etrade.com/authorize?token=abc" in r for r in replies)
        mock_etrade.mark_prompt_sent.assert_called_once_with(
            channel="telegram", chat_id=12345, prompt_message_id=555
        )

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    async def test_update_etrade_auth_start_failure(self, mock_etrade, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_etrade.start_pin_auth.side_effect = RuntimeError("boom")
        rl = RateLimiter(10, 60)
        update = _make_update(text="/update-etrade-auth")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_etrade.mark_prompt_sent.assert_not_called()
        reply = update.message.reply_text.call_args[0][0]
        assert "Failed to start E*TRADE login" in reply

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    async def test_genuine_reply_to_prompt_completes_login_and_triggers_retry_check(
        self, mock_etrade, mock_config
    ):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.ETRADE_CONSUMER_KEY = "key"
        mock_config.ETRADE_CONSUMER_SECRET = "secret"
        mock_config.ETRADE_MODE = "production"
        pending = {"channel": "telegram", "chat_id": 12345, "prompt_message_id": 555, "session_key": "12345"}
        mock_etrade.load_pending.return_value = pending
        mock_etrade.finish_pin_auth.return_value = {"access_token": "AT", "access_token_secret": "AS"}
        rl = RateLimiter(10, 60)
        update = _make_update(text="123456")
        update.message.reply_to_message = MagicMock(message_id=555)
        context = MagicMock()
        context.background_tasks = set()

        await handle_message(update, context, rl, MagicMock(spec=SessionManager))
        if context.background_tasks:
            await asyncio.gather(*context.background_tasks)

        mock_etrade.finish_pin_auth.assert_called_once_with(pending, "123456", "key", "secret")
        mock_etrade.save_access_token.assert_called_once_with("AT", "AS", sandbox=False)
        mock_etrade.clear_pending.assert_called_once()
        mock_etrade.complete_and_maybe_retry.assert_called_once_with(pending)
        replies = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("successful" in r.lower() for r in replies)

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    async def test_manual_update_etrade_auth_pending_never_triggers_retry(self, mock_etrade, mock_config):
        """/update-etrade-auth's own pending record has no session_key, so
        completing it must never call complete_and_maybe_retry with
        anything to retry — this is exercised end-to-end via the pending
        dict shape /update-etrade-auth actually produces (Task 5's
        etrade_cli.py test suite covers that no SYNAPSE_* fields leak in;
        this test just confirms the listener always defers the decision
        to complete_and_maybe_retry rather than special-casing it)."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.ETRADE_CONSUMER_KEY = "key"
        mock_config.ETRADE_CONSUMER_SECRET = "secret"
        mock_config.ETRADE_MODE = "production"
        pending = {"channel": "telegram", "chat_id": 12345, "prompt_message_id": 555}  # no session_key
        mock_etrade.load_pending.return_value = pending
        mock_etrade.finish_pin_auth.return_value = {"access_token": "AT", "access_token_secret": "AS"}
        rl = RateLimiter(10, 60)
        update = _make_update(text="123456")
        update.message.reply_to_message = MagicMock(message_id=555)
        context = MagicMock()
        context.background_tasks = set()

        await handle_message(update, context, rl, MagicMock(spec=SessionManager))
        if context.background_tasks:
            await asyncio.gather(*context.background_tasks)

        mock_etrade.complete_and_maybe_retry.assert_called_once_with(pending)

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    async def test_finish_failure_still_clears_pending_and_reports_error(self, mock_etrade, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        pending = {"channel": "telegram", "chat_id": 12345, "prompt_message_id": 555}
        mock_etrade.load_pending.return_value = pending
        mock_etrade.finish_pin_auth.side_effect = RuntimeError("invalid verifier")
        rl = RateLimiter(10, 60)
        update = _make_update(text="wrong-code")
        update.message.reply_to_message = MagicMock(message_id=555)

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_etrade.clear_pending.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "failed" in reply.lower()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_reply_to_wrong_message_is_not_intercepted(self, mock_pipe, mock_etrade, mock_config):
        """A reply to some other message while a pending auth exists must
        not be swallowed as the code — this is the 'don't leak into the
        general session' scoping requirement."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        pending = {"channel": "telegram", "chat_id": 12345, "prompt_message_id": 555}
        mock_etrade.load_pending.return_value = pending
        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=False, output="", session_id=None, stats={}
        )
        rl = RateLimiter(10, 60)
        update = _make_update(text="unrelated reply")
        update.message.reply_to_message = MagicMock(message_id=999)  # not the prompt message

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_etrade.finish_pin_auth.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.etrade_pin_auth")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_reply_from_different_chat_is_not_intercepted(self, mock_pipe, mock_etrade, mock_config):
        """A pending auth for one chat must not be completable by a reply
        from a different chat, even to the right message_id."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345, 99999]
        pending = {"channel": "telegram", "chat_id": 99999, "prompt_message_id": 555}
        mock_etrade.load_pending.return_value = pending
        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=False, output="", session_id=None, stats={}
        )
        rl = RateLimiter(10, 60)
        update = _make_update(user_id=12345, text="123456")
        update.message.reply_to_message = MagicMock(message_id=555)

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_etrade.finish_pin_auth.assert_not_called()


@patch("services.ingestion.channels.telegram.listener.config")
class TestFormDispatch:
    """Tests for Actionable Form dispatch in handle_message and the quota-retry callback."""

    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments")
    async def test_response_with_form_gets_form_keyboard(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.TELEGRAM_MAX_FILE_SIZE_MB = 10
        mock_extract.return_value = []

        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=False,
            output="Check-in:\n☐F:yn:protein Protein at every meal?",
            session_id="test-session", stats=None,
        )

        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = False
        rl = RateLimiter(10, 60)

        update = _make_update(text="send check-in")
        update.message.reply_to_message = None
        mock_sent = MagicMock()
        mock_sent.message_id = 777
        update.message.reply_text.return_value = mock_sent

        await handle_message(update, None, rl, sm)

        call_args, call_kwargs = update.message.reply_text.call_args
        assert "☐F:" not in call_args[0]
        keyboard = call_kwargs["reply_markup"]
        assert any("formyn:" in b.callback_data for row in keyboard.inline_keyboard for b in row)

    @patch("services.ingestion.core.form_state.delete_form")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments")
    async def test_send_failure_cleans_up_form(self, mock_extract, mock_pipe, mock_delete_form, mock_config):
        """An unrecoverable send failure must not leave the form dangling in form_state."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.TELEGRAM_MAX_FILE_SIZE_MB = 10
        mock_extract.return_value = []

        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=False,
            output="☐F:yn:protein Protein at every meal?",
            session_id="test-session", stats=None,
        )

        sm = MagicMock(spec=SessionManager)
        sm.get_session.return_value = None
        sm.get_stats_enabled.return_value = False
        rl = RateLimiter(10, 60)

        update = _make_update(text="send check-in")
        update.message.reply_to_message = None
        update.message.reply_text.side_effect = BadRequest("network error")

        with pytest.raises(BadRequest):
            await handle_message(update, None, rl, sm)

        mock_delete_form.assert_called_once()

    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_quota_retry_resolves_into_form(self, mock_pipe, mock_config):
        """A quota-retry response with ☐F: fields must render as a form, not raw text."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        listener._pending_retries[555] = {
            "prompt": "some prompt",
            "session_id": None,
            "user_key": "12345",
        }

        mock_pipe.return_value = MagicMock(
            is_error=False, output="☐F:yn:protein Protein at every meal?",
            session_id="sess-1", stats=None,
        )

        update = MagicMock()
        update.callback_query.data = "quota:retry"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.message_id = 555
        update.callback_query.message.chat.id = 12345
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.edit_text = AsyncMock()

        sm = MagicMock(spec=SessionManager)
        sm.get_stats_enabled.return_value = False

        await handle_callback_query(update, None, sm)

        edit_args, edit_kwargs = update.callback_query.message.edit_text.call_args
        assert "☐F:" not in edit_args[0]
        keyboard = edit_kwargs["reply_markup"]
        assert any("formyn:" in b.callback_data for row in keyboard.inline_keyboard for b in row)

    @patch("services.ingestion.core.form_state.delete_form")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_provider")
    async def test_quota_retry_edit_failure_cleans_up_form(self, mock_pipe, mock_delete_form, mock_config):
        """A failed final edit_text in the quota-retry path must not leave the form dangling."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        listener._pending_retries[556] = {
            "prompt": "some prompt",
            "session_id": None,
            "user_key": "12345",
        }

        mock_pipe.return_value = MagicMock(
            is_error=False, output="☐F:yn:protein Protein at every meal?",
            session_id="sess-1", stats=None,
        )

        update = MagicMock()
        update.callback_query.data = "quota:retry"
        update.callback_query.from_user.id = 12345
        update.callback_query.message.message_id = 556
        update.callback_query.message.chat.id = 12345
        update.callback_query.answer = AsyncMock()
        # "⏳ Processing..." edit succeeds; the final edit with the keyboard fails
        update.callback_query.message.edit_text = AsyncMock(side_effect=[None, Exception("boom")])

        sm = MagicMock(spec=SessionManager)
        sm.get_stats_enabled.return_value = False

        await handle_callback_query(update, None, sm)  # must not raise

        mock_delete_form.assert_called_once()
