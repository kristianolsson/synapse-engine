"""
Tests for the ReminderScheduler core logic.

Verifies prompt building, JSON parsing, routing, work re-pipe,
and delivery failure fallback.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

from services.ingestion.core.scheduler import ReminderScheduler


@pytest.fixture
def scheduler():
    """Create a scheduler instance with test defaults."""
    return ReminderScheduler(interval_minutes=60)


class TestBuildSchedulerPrompt:
    """Tests for _build_scheduler_prompt."""

    def test_prompt_contains_current_time(self, scheduler):
        prompt = scheduler._build_scheduler_prompt()
        now = datetime.now()
        assert now.strftime("%Y-%m-%dT%H:") in prompt

    def test_prompt_contains_hour_window(self, scheduler):
        prompt = scheduler._build_scheduler_prompt()
        now = datetime.now()
        assert now.strftime("%Y-%m-%dT%H:00:00") in prompt
        assert now.strftime("%Y-%m-%dT%H:59:59") in prompt

    def test_prompt_mentions_reminders_md(self, scheduler):
        prompt = scheduler._build_scheduler_prompt()
        assert "reminders.md" in prompt

    def test_prompt_asks_for_json(self, scheduler):
        prompt = scheduler._build_scheduler_prompt()
        assert "JSON" in prompt


class TestParseResponse:
    """Tests for _parse_response."""

    def test_empty_string(self, scheduler):
        assert scheduler._parse_response("") == []

    def test_none_input(self, scheduler):
        assert scheduler._parse_response(None) == []

    def test_empty_json_array(self, scheduler):
        assert scheduler._parse_response("[]") == []

    def test_valid_message_type(self, scheduler):
        response = json.dumps([
            {"type": "message", "channel": "telegram", "message": "Call the dentist!"}
        ])
        result = scheduler._parse_response(response)
        assert len(result) == 1
        assert result[0] == {
            "type": "message",
            "channel": "telegram",
            "message": "Call the dentist!",
        }

    def test_valid_work_type(self, scheduler):
        response = json.dumps([
            {"type": "work", "channel": "email", "task": "Research mortgage rates"}
        ])
        result = scheduler._parse_response(response)
        assert len(result) == 1
        assert result[0] == {
            "type": "work",
            "channel": "email",
            "task": "Research mortgage rates",
        }

    def test_mixed_types(self, scheduler):
        response = json.dumps([
            {"type": "message", "channel": "telegram", "message": "Reminder 1"},
            {"type": "work", "channel": "telegram", "task": "Do research"},
            {"type": "message", "channel": "email", "message": "Reminder 2"},
        ])
        result = scheduler._parse_response(response)
        assert len(result) == 3

    def test_invalid_json(self, scheduler):
        result = scheduler._parse_response("not json at all")
        assert result == []

    def test_json_not_array(self, scheduler):
        result = scheduler._parse_response('{"type": "message"}')
        assert result == []

    def test_invalid_item_skipped(self, scheduler):
        response = json.dumps([
            {"type": "message", "channel": "telegram", "message": "Valid"},
            {"type": "unknown", "channel": "telegram"},
            "just a string",
            {"type": "message"},  # missing "message" key
        ])
        result = scheduler._parse_response(response)
        assert len(result) == 1
        assert result[0]["message"] == "Valid"

    def test_markdown_code_fences_stripped(self, scheduler):
        response = '```json\n[{"type": "message", "channel": "telegram", "message": "Test"}]\n```'
        result = scheduler._parse_response(response)
        assert len(result) == 1
        assert result[0]["message"] == "Test"

    def test_default_channel(self, scheduler):
        """Channel defaults to 'telegram' when not specified."""
        response = json.dumps([
            {"type": "message", "message": "No channel specified"}
        ])
        result = scheduler._parse_response(response)
        assert len(result) == 1
        assert result[0]["channel"] == "telegram"


class TestDelivery:
    """Tests for _deliver, _send_telegram, _send_email."""

    @patch("services.ingestion.core.scheduler.config")
    def test_send_telegram(self, mock_config, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        with patch(
            "services.ingestion.channels.telegram.sender.send_telegram_message"
        ) as mock_send:
            mock_send.return_value = True
            result = scheduler._send_telegram("Hello!")
            mock_send.assert_called_once_with(12345, "Hello!")
            assert result is True

    @patch("services.ingestion.core.scheduler.config")
    def test_send_telegram_no_user_ids(self, mock_config, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = []
        result = scheduler._send_telegram("Hello!")
        assert result is False

    @patch("services.ingestion.core.scheduler.config")
    def test_send_email(self, mock_config, scheduler):
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        with patch(
            "services.ingestion.channels.email.reply.send_reply"
        ) as mock_send:
            mock_send.return_value = True
            result = scheduler._send_email("Hello!", subject="Synapse: Hello!", session_id="test-session-123")
            mock_send.assert_called_once_with(
                to_addr="user@example.com",
                subject="Synapse: Hello!",
                body="Hello!",
                message_id="<test-session-123@synapse.local>",
            )
            assert result is True

    @patch("services.ingestion.core.scheduler.config")
    def test_send_email_no_address(self, mock_config, scheduler):
        mock_config.REPLY_TO_ADDRESS = ""
        mock_config.ALLOWED_SENDERS = []
        result = scheduler._send_email("Hello!", subject="Synapse: Hello!")
        assert result is False

    def test_deliver_unknown_channel(self, scheduler):
        result = scheduler._deliver("sms", "Hello!")
        assert result is False


class TestTick:
    """Tests for the _tick method (one scheduler cycle)."""

    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    def test_tick_with_messages(self, mock_pipe, mock_deliver, scheduler):
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output=json.dumps([
                {"type": "message", "channel": "telegram", "message": "Reminder 1"},
                {"type": "message", "channel": "email", "message": "Reminder 2"},
            ]),
        )
        mock_deliver.return_value = True

        scheduler._tick()

        assert mock_deliver.call_count == 2
        calls = [c.args for c in mock_deliver.call_args_list]
        channels = [c[0] for c in calls]
        messages = [c[1] for c in calls]
        assert "telegram" in channels
        assert "email" in channels
        # Telegram messages get prefixed with "Reminder: "
        assert "Reminder: Reminder 1" in messages
        # Email messages are unchanged
        assert "Reminder 2" in messages

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    def test_tick_no_reminders(self, mock_pipe, scheduler):
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output="[]",
        )
        scheduler._tick()
        # No errors, no delivery attempts

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    def test_tick_prompt_error(self, mock_pipe, scheduler):
        mock_pipe.return_value = MagicMock(
            is_error=True,
            output="API error",
        )
        # Should not raise
        scheduler._tick()

    @patch.object(ReminderScheduler, "_handle_work_reminder")
    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    def test_tick_with_work_reminder(self, mock_pipe, mock_work, scheduler):
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output=json.dumps([
                {"type": "work", "channel": "telegram", "task": "Research stocks"},
            ]),
        )

        scheduler._tick()

        mock_work.assert_called_once_with("telegram", "Research stocks")

    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    def test_tick_delivery_failure_triggers_fallback(
        self, mock_pipe, mock_deliver, mock_fallback, scheduler
    ):
        mock_pipe.return_value = MagicMock(
            is_error=False,
            output=json.dumps([
                {"type": "message", "channel": "telegram", "message": "Test"},
            ]),
        )
        mock_deliver.return_value = False

        scheduler._tick()

        mock_fallback.assert_called_once_with("Test")


class TestWorkReminder:
    """Tests for _handle_work_reminder."""

    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_telegram(self, mock_config, mock_pipe, mock_deliver, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        mock_pipe.return_value = MagicMock(
            is_error=False,
            output="Here are the stock results...",
        )
        mock_deliver.return_value = True

        scheduler._handle_work_reminder("telegram", "Research stocks")

        # Should pipe the task through ingestion
        mock_pipe.assert_called_once()
        prompt_arg = mock_pipe.call_args[0][0]
        assert "Research stocks" in prompt_arg
        assert "telegram" in prompt_arg.lower()

        # Should deliver the result
        assert mock_deliver.call_count == 1
        call_args = mock_deliver.call_args
        assert call_args.args[0] == "telegram"
        assert call_args.args[1] == "Here are the stock results..."

    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_pipe_error(self, mock_config, mock_pipe, mock_fallback, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        mock_pipe.return_value = MagicMock(
            is_error=True,
            output="API error",
        )

        scheduler._handle_work_reminder("telegram", "Research stocks")

        mock_fallback.assert_called_once_with("Research stocks")


class TestDeliveryFailure:
    """Tests for _handle_delivery_failure."""

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    def test_fallback_logs_to_master_todos(self, mock_pipe, scheduler):
        mock_pipe.return_value = MagicMock(is_error=False, output="")

        scheduler._handle_delivery_failure("Call the dentist")

        mock_pipe.assert_called_once()
        prompt_arg = mock_pipe.call_args[0][0]
        assert "master_todos" in prompt_arg
        assert "Call the dentist" in prompt_arg

    @patch("services.ingestion.core.scheduler.pipe_to_gemini")
    def test_fallback_handles_exception(self, mock_pipe, scheduler):
        mock_pipe.side_effect = Exception("Total failure")
        # Should not raise
        scheduler._handle_delivery_failure("Call the dentist")
