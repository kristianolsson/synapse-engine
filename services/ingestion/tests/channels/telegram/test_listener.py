"""Tests for the Telegram listener module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from services.ingestion import config
from services.ingestion.core.rate_limiter import RateLimiter
from services.ingestion.core.session_manager import SessionManager
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

        update.message.reply_text.assert_called_once_with("✓", parse_mode='HTML', reply_markup=None)

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

        update.message.reply_text.assert_called_once_with("Repo is locked", parse_mode='HTML', reply_markup=None)

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


@pytest.mark.asyncio
@patch("services.ingestion.channels.telegram.listener.config")
class TestTaskButtons:
    """Tests for inline keyboard attachment and callback query handling."""

    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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

    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
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

    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
    async def test_callback_query_completes_task(self, mock_pipe, mock_config):
        """Tapping a ✅ button pipes completion to Gemini and removes the button."""
        from services.ingestion.channels.telegram.task_buttons import _hash_task

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

        await handle_callback_query(update, None, sm)

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

    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
    async def test_callback_query_undo_task(self, mock_pipe, mock_config):
        from services.ingestion.channels.telegram.task_buttons import _hash_task
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

        await handle_callback_query(update, None, sm)

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

    @patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
    async def test_callback_query_rollback_on_error(self, mock_pipe, mock_config):
        """If Gemini fails, the optimistic UI update should be rolled back."""
        from services.ingestion.channels.telegram.task_buttons import _hash_task
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

        await handle_callback_query(update, None, sm)

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
