"""
Tests for session persistence in ReminderScheduler.
"""

import json
from unittest.mock import MagicMock, patch
import pytest
import uuid

from services.ingestion.core.scheduler import ReminderScheduler

@pytest.fixture
def scheduler():
    """Create a scheduler instance with test defaults."""
    return ReminderScheduler(interval_minutes=60)

class TestSchedulerSessions:
    """Tests for session persistence logic in ReminderScheduler."""

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    @patch("services.ingestion.core.session_manager.SessionManager.save_session")
    def test_tick_saves_session_for_message_email(self, mock_save, mock_pipe, scheduler):
        """Test that _tick saves a session ID for email message reminders."""
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output=json.dumps([
                {"type": "message", "channel": "email", "message": "Email Reminder"},
            ]),
        )
        
        with patch("services.ingestion.core.scheduler.ReminderScheduler._deliver") as mock_deliver:
            mock_deliver.return_value = True
            scheduler._tick()
            
            # Should NOT have called save_session for the scheduler prompt
            assert mock_save.call_count == 0
            
            # Should have delivered with session_id=None
            mock_deliver.assert_called_once()
            assert mock_deliver.call_args[1].get("session_id") is None

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    @patch("services.ingestion.core.session_manager.SessionManager.save_session")
    def test_tick_does_not_save_session_for_message_telegram(self, mock_save, mock_pipe, scheduler):
        """Test that _tick does NOT save a session ID for telegram message reminders."""
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output=json.dumps([
                {"type": "message", "channel": "telegram", "message": "Telegram Reminder"},
            ]),
        )
        
        with patch("services.ingestion.core.scheduler.ReminderScheduler._deliver") as mock_deliver:
            mock_deliver.return_value = True
            scheduler._tick()
            
            # Should NOT have called save_session
            assert mock_save.call_count == 0

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    @patch("services.ingestion.core.session_manager.SessionManager.save_session")
    @patch("services.ingestion.core.scheduler.config")
    def test_handle_work_reminder_saves_session_for_email(self, mock_config, mock_save, mock_pipe, scheduler):
        """Test that _handle_work_reminder saves the session ID for email."""
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]
        
        returned_session_id = "gemini-session-123"
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output="Work complete",
            session_id=returned_session_id
        )
        
        with patch("services.ingestion.core.scheduler.ReminderScheduler._deliver") as mock_deliver:
            mock_deliver.return_value = True
            scheduler._handle_work_reminder("email", "Do some work")
            
            # Should have saved session
            mock_save.assert_called_once_with(
                f"<{returned_session_id}@synapse.local>",
                returned_session_id
            )
            
            # Should have delivered with session_id
            mock_deliver.assert_called_once()
            assert mock_deliver.call_args[1]["session_id"] == returned_session_id

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    @patch("services.ingestion.core.session_manager.SessionManager.save_session")
    @patch("services.ingestion.core.scheduler.config")
    def test_handle_work_reminder_no_save_for_telegram(self, mock_config, mock_save, mock_pipe, scheduler):
        """Test that _handle_work_reminder does NOT save session for telegram."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        
        returned_session_id = "gemini-session-123"
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output="Work complete",
            session_id=returned_session_id
        )
        
        with patch("services.ingestion.core.scheduler.ReminderScheduler._deliver") as mock_deliver:
            mock_deliver.return_value = True
            scheduler._handle_work_reminder("telegram", "Do some work")
            
            # Should NOT have saved session
            assert mock_save.call_count == 0
