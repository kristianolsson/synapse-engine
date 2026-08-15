"""
Tests for transient error handling (quota, timeout) in the Telegram listener.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from services.ingestion.services.telegram.listener import handle_message
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
@patch("services.ingestion.services.telegram.listener.config")
@patch("services.ingestion.services.telegram.listener.get_next_provider")
@patch("services.ingestion.services.telegram.listener.pipe_to_provider")
@patch("services.ingestion.services.telegram.listener.extract_attachments", new_callable=AsyncMock)
async def test_timeout_triggers_retry_buttons(mock_extract, mock_pipe, mock_next_provider, mock_config):
    """Verify that a timeout error triggers retry button (no fallback when single provider)."""
    mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
    mock_config.get_ai_provider.return_value = "gemini"
    mock_next_provider.return_value = None  # No fallback
    mock_extract.return_value = []
    
    # Simulate a timeout error
    mock_pipe.return_value = MagicMock(
        is_error=True, 
        requires_reply=True, 
        output="Gemini CLI timed out after 300 seconds.", 
        session_id="test-session", 
        stats=None,
        provider_name="gemini",
    )
    
    rl = RateLimiter(10, 60)
    sm = MagicMock(spec=SessionManager)
    sm.get_session.return_value = None
    
    update = _make_update()
    
    await handle_message(update, None, rl, sm)
    
    # Verify reply_text was called with the correct label and buttons
    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "<b>Gemini hit a limit</b>" in args[0]
    assert "timed out" in args[0]
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["reply_markup"] is not None
    
    # Only retry button, no fallback
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    assert len(keyboard[0]) == 1
    assert keyboard[0][0].text == "🔁 Retry"
    assert keyboard[0][0].callback_data == "quota:retry"

@pytest.mark.asyncio
@patch("services.ingestion.services.telegram.listener.config")
@patch("services.ingestion.services.telegram.listener.get_next_provider")
@patch("services.ingestion.services.telegram.listener.pipe_to_provider")
@patch("services.ingestion.services.telegram.listener.extract_attachments", new_callable=AsyncMock)
async def test_quota_triggers_retry_buttons(mock_extract, mock_pipe, mock_next_provider, mock_config):
    """Verify that a quota error triggers the retry buttons."""
    mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
    mock_config.get_ai_provider.return_value = "gemini"
    mock_next_provider.return_value = None
    mock_extract.return_value = []
    
    # Simulate a quota error
    mock_pipe.return_value = MagicMock(
        is_error=True, 
        requires_reply=True, 
        output="Quota Error: Exhausted", 
        session_id="test-session", 
        stats=None,
        provider_name="gemini",
    )
    
    rl = RateLimiter(10, 60)
    sm = MagicMock(spec=SessionManager)
    sm.get_session.return_value = None
    
    update = _make_update()
    
    await handle_message(update, None, rl, sm)
    
    # Verify reply_text was called with the correct label
    update.message.reply_text.assert_called_once()
    args, _ = update.message.reply_text.call_args
    assert "<b>Gemini hit a limit</b>" in args[0]
    assert "Quota Error" in args[0]

@pytest.mark.asyncio
@patch("services.ingestion.services.telegram.listener.config")
@patch("services.ingestion.services.telegram.listener.get_next_provider")
@patch("services.ingestion.services.telegram.listener.pipe_to_provider")
@patch("services.ingestion.services.telegram.listener.extract_attachments", new_callable=AsyncMock)
async def test_claude_quota_shows_fallback_button(mock_extract, mock_pipe, mock_next_provider, mock_config):
    """When Claude hits quota and gemini is next in the list, show a 'Try with gemini' button."""
    mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
    mock_config.get_ai_provider.return_value = "claude"
    mock_next_provider.return_value = "gemini"  # Fallback available
    mock_extract.return_value = []
    
    mock_pipe.return_value = MagicMock(
        is_error=True,
        requires_reply=True,
        output="Quota limit reached: You've hit your limit · resets 12am",
        session_id="test-session",
        stats=None,
        provider_name="claude",
    )
    
    rl = RateLimiter(10, 60)
    sm = MagicMock(spec=SessionManager)
    sm.get_session.return_value = None
    
    update = _make_update()
    await handle_message(update, None, rl, sm)
    
    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    
    assert "<b>Claude hit a limit</b>" in args[0]
    assert "hit your limit" in args[0]
    
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    assert len(keyboard[0]) == 2  # Retry + fallback
    assert keyboard[0][0].text == "🔁 Retry"
    assert keyboard[0][0].callback_data == "quota:retry"
    assert keyboard[0][1].text == "🔄 Try with gemini"
    assert keyboard[0][1].callback_data == "quota:switch:gemini"

@pytest.mark.asyncio
@patch("services.ingestion.services.telegram.listener.config")
@patch("services.ingestion.services.telegram.listener.get_next_provider")
@patch("services.ingestion.services.telegram.listener.pipe_to_provider")
@patch("services.ingestion.services.telegram.listener.extract_attachments", new_callable=AsyncMock)
async def test_claude_quota_no_fallback_when_single_provider(mock_extract, mock_pipe, mock_next_provider, mock_config):
    """When Claude is the only provider, no fallback button is shown."""
    mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
    mock_config.get_ai_provider.return_value = "claude"
    mock_next_provider.return_value = None  # No fallback
    mock_extract.return_value = []
    
    mock_pipe.return_value = MagicMock(
        is_error=True,
        requires_reply=True,
        output="Quota limit reached: You've hit your limit · resets 12am",
        session_id="test-session",
        stats=None,
        provider_name="claude",
    )
    
    rl = RateLimiter(10, 60)
    sm = MagicMock(spec=SessionManager)
    sm.get_session.return_value = None
    
    update = _make_update()
    await handle_message(update, None, rl, sm)
    
    update.message.reply_text.assert_called_once()
    _, kwargs = update.message.reply_text.call_args
    
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert len(keyboard[0]) == 1  # Only retry, no fallback
    assert keyboard[0][0].callback_data == "quota:retry"
