"""Tests for the Telegram listener module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from services.ingestion import config
from services.ingestion.core.rate_limiter import RateLimiter
from services.ingestion.core.session_manager import SessionManager
from services.ingestion.channels.telegram.listener import (
    handle_message,
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
    async def test_rejects_group_chat(self, mock_pipe, mock_config):
        self.setUp()
        update = _make_update(chat_type="group")

        await handle_message(update, None, self.rate_limiter, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
    async def test_rejects_unauthorized_user(self, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [99999]
        rl = RateLimiter(10, 60)
        update = _make_update(user_id=12345)

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
    @patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_replies_checkmark(self, mock_extract, mock_pipe, mock_config):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)
        rl = RateLimiter(10, 60)
        update = _make_update(text="Buy groceries")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        update.message.reply_text.assert_called_once_with("✓", parse_mode='HTML')

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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

        update.message.reply_text.assert_called_once_with("Repo is locked", parse_mode='HTML')

    @pytest.mark.asyncio
    @patch("services.ingestion.channels.telegram.listener.config")
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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
        
        # Verify pipe_to_gemini was called with the linked session and injected context
        args, kwargs = mock_pipe.call_args
        actual_prompt = args[0]
        actual_session_id = kwargs.get("session_id") if "session_id" in kwargs else args[1]
        
        assert actual_session_id == "linked-session"
        assert "Context: You previously sent the user this message: \"Original reminder\"" in actual_prompt
        assert "The user replied to that message with: \"Following up on that\"" in actual_prompt
        
        # Verify the NEW message ID (1000) was saved to the NEW session ID returned by pipe
        sm.save_message_session.assert_called_once_with(1000, "new-session")
