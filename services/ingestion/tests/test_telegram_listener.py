"""Tests for the Telegram listener module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from services.ingestion import config
from services.ingestion.rate_limiter import RateLimiter
from services.ingestion.session_manager import SessionManager
from services.ingestion.telegram_listener import (
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
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    async def test_rejects_group_chat(self, mock_pipe, mock_config):
        self.setUp()
        update = _make_update(chat_type="group")

        await handle_message(update, None, self.rate_limiter, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    async def test_rejects_unauthorized_user(self, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [99999]
        rl = RateLimiter(10, 60)
        update = _make_update(user_id=12345)

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    async def test_accepts_authorized_user(self, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="")
        rl = RateLimiter(10, 60)
        update = _make_update(user_id=12345, text="Add todo")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_called_once()


# ── Rate Limiting tests ────────────────────────────────────────────


class TestTelegramRateLimiting:
    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    async def test_rate_limit_blocks_message(self, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

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
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    @patch("services.ingestion.telegram_listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_replies_checkmark(self, mock_extract, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="", stats=None)
        rl = RateLimiter(10, 60)
        update = _make_update(text="Buy groceries")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        update.message.reply_text.assert_called_once_with("✓")

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    @patch("services.ingestion.telegram_listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_with_stats(self, mock_extract, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        
        # Mock pipe result with stats
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
        update = _make_update(text="Analyze this")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        # Check if reply contains the original output AND the stats
        args = update.message.reply_text.call_args[0][0]
        assert "Done" in args
        assert "gemini-pro (2 req, 0 err)" in args

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    @patch("services.ingestion.telegram_listener.extract_attachments", new_callable=AsyncMock)
    async def test_success_with_tool_stats(self, mock_extract, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

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
        update = _make_update(text="Analyze this")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        args = update.message.reply_text.call_args[0][0]
        assert "Done" in args
        assert "gemini-pro (1 req, 0 err)" in args
        assert "google_web_search: 2 (1 ok, 1 fail)" in args

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    @patch("services.ingestion.telegram_listener.extract_attachments", new_callable=AsyncMock)
    async def test_error_replies_with_output(self, mock_extract, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=True, output="Repo is locked", session_id="", stats=None)
        rl = RateLimiter(10, 60)
        update = _make_update(text="Add todo")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        update.message.reply_text.assert_called_once_with("Repo is locked")

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    @patch("services.ingestion.telegram_listener.extract_attachments", new_callable=AsyncMock)
    async def test_empty_message_ignored(self, mock_extract, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        rl = RateLimiter(10, 60)
        update = _make_update(text="")
        update.message.text = ""
        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    @patch("services.ingestion.telegram_listener.extract_attachments", new_callable=AsyncMock)
    async def test_voice_message_replies_unsupported(self, mock_extract, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        rl = RateLimiter(10, 60)
        
        update = _make_update(text="")
        update.message.voice = MagicMock()

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        mock_pipe.assert_not_called()
        update.message.reply_text.assert_called_once_with("Sorry, voice notes are not supported yet.")

    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.config")
    @patch("services.ingestion.telegram_listener.pipe_to_gemini")
    @patch("services.ingestion.telegram_listener.extract_attachments", new_callable=AsyncMock)
    async def test_prompt_contains_telegram_type(self, mock_extract, mock_pipe, mock_config):
        from services.ingestion.telegram_listener import handle_message

        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_extract.return_value = []
        mock_pipe.return_value = MagicMock(is_error=False, requires_reply=False, output="", session_id="")
        rl = RateLimiter(10, 60)
        update = _make_update(text="Hello")

        await handle_message(update, None, rl, MagicMock(spec=SessionManager))

        prompt_arg = mock_pipe.call_args[0][0]
        assert "Type: telegram" in prompt_arg
        assert "Sender: 12345" in prompt_arg


# ── Attachment tests ────────────────────────────────────────────────


class TestTelegramAttachments:
    @pytest.mark.asyncio
    @patch("services.ingestion.telegram_listener.MAX_FILE_BYTES", 1 * 1024 * 1024)
    @patch("services.ingestion.telegram_listener.config")
    async def test_download_skips_large_file(self, mock_config, tmp_path):
        from services.ingestion.telegram_listener import download_attachment

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
    @patch("services.ingestion.telegram_listener.config")
    async def test_download_saves_file(self, mock_config, tmp_path):
        from services.ingestion.telegram_listener import download_attachment

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
