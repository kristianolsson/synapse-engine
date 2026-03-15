"""
Tests for transient error handling (quota, timeout) in the Telegram listener.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from services.ingestion.channels.telegram.listener import handle_message
from services.ingestion.core.rate_limiter import RateLimiter
from services.ingestion.core.session_manager import SessionManager

def _make_update(user_id=12345, chat_type="private", text="Hello"):
    update = MagicMock()
    update.message.from_user.id = user_id
    update.message.chat.type = chat_type
    update.message.chat.id = user_id
    update.message.text = text
    update.message.caption = None
    update.message.photo = []
    update.message.document = None
    update.message.voice = None
    update.message.reply_text = AsyncMock()
    update.get_bot.return_value = MagicMock()
    return update

@pytest.mark.asyncio
@patch("services.ingestion.channels.telegram.listener.config")
@patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
@patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
async def test_timeout_triggers_retry_buttons(mock_extract, mock_pipe, mock_config):
    """Verify that a timeout error triggers the 'Request Failed' buttons."""
    mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
    mock_extract.return_value = []
    
    # Simulate a timeout error
    mock_pipe.return_value = MagicMock(
        is_error=True, 
        requires_reply=True, 
        output="Gemini CLI timed out after 300 seconds.", 
        session_id="test-session", 
        stats=None
    )
    
    rl = RateLimiter(10, 60)
    sm = MagicMock(spec=SessionManager)
    sm.get_session.return_value = None
    
    update = _make_update()
    
    await handle_message(update, None, rl, sm)
    
    # Verify reply_text was called with the correct label and buttons
    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "⚠️ <b>Request Failed</b>" in args[0]
    assert "timed out" in args[0]
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["reply_markup"] is not None
    
    # Verify buttons
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    assert keyboard[0][0].text == "Retry (Same Session)"
    assert keyboard[0][0].callback_data == "quota:retry"
    assert keyboard[0][1].text == "Fallback (New Session)"
    assert keyboard[0][1].callback_data == "quota:fallback"

@pytest.mark.asyncio
@patch("services.ingestion.channels.telegram.listener.config")
@patch("services.ingestion.channels.telegram.listener.pipe_to_gemini")
@patch("services.ingestion.channels.telegram.listener.extract_attachments", new_callable=AsyncMock)
async def test_quota_triggers_retry_buttons(mock_extract, mock_pipe, mock_config):
    """Verify that a quota error triggers the 'Request Failed' buttons."""
    mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
    mock_extract.return_value = []
    
    # Simulate a quota error
    mock_pipe.return_value = MagicMock(
        is_error=True, 
        requires_reply=True, 
        output="Quota Error: Exhausted", 
        session_id="test-session", 
        stats=None
    )
    
    rl = RateLimiter(10, 60)
    sm = MagicMock(spec=SessionManager)
    sm.get_session.return_value = None
    
    update = _make_update()
    
    await handle_message(update, None, rl, sm)
    
    # Verify reply_text was called with the correct label
    update.message.reply_text.assert_called_once()
    args, _ = update.message.reply_text.call_args
    assert "⚠️ <b>Request Failed</b>" in args[0]
    assert "Quota Error" in args[0]
