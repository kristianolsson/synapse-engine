"""
Tests for session persistence in ReminderScheduler.
"""

from unittest.mock import MagicMock, patch
import pytest

from services.ingestion.core.scheduler import ReminderScheduler
from services.ingestion.core.session_manager import SessionManager

@pytest.fixture
def scheduler():
    """Create a scheduler instance with test defaults. Uses a real
    SessionManager (not a MagicMock) so the class-level
    SessionManager.save_session patches below actually intercept calls made
    through scheduler.session_manager."""
    return ReminderScheduler(session_manager=SessionManager())

class TestSchedulerSessions:
    """Tests for session persistence logic in ReminderScheduler."""

    @patch("services.ingestion.core.scheduler.pipe_to_provider")
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

    @patch("services.ingestion.core.scheduler.pipe_to_provider")
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
