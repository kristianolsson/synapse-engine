"""
Tests for the ReminderScheduler core logic.

Verifies compute_next_fire, scan/schedule, dispatch, delivery, and
failure handling for the refactored two-tier scheduler.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, mock_open
from zoneinfo import ZoneInfo

import pytest

from services.ingestion.core.scheduler import (
    ReminderScheduler,
    compute_next_fire,
    MISSED_THRESHOLD_SECONDS,
)

LOCAL_TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def scheduler():
    """Create a scheduler instance for testing."""
    return ReminderScheduler()


# ── compute_next_fire ────────────────────────────────────────────────


class TestComputeNextFire:
    """Tests for the compute_next_fire function."""

    def test_one_shot_future(self):
        reminder = {"time": "2099-06-22T07:00:00", "recurring": "none"}
        result = compute_next_fire(reminder)
        assert result is not None
        assert result.year == 2099
        assert result.month == 6
        assert result.day == 22
        assert result.hour == 7

    def test_one_shot_past(self):
        """One-shot reminders return their exact time even if past (for missed detection)."""
        reminder = {"time": "2020-01-01T07:00:00", "recurring": "none"}
        result = compute_next_fire(reminder)
        assert result is not None
        assert result.year == 2020

    def test_one_shot_invalid_format(self):
        reminder = {"time": "not-a-date", "recurring": "none"}
        result = compute_next_fire(reminder)
        assert result is None

    def test_daily_future_today(self):
        """If the time hasn't passed today, fires today."""
        now = datetime.now(LOCAL_TZ)
        future_time = (now + timedelta(hours=2)).strftime("%H:%M")
        reminder = {"time": future_time, "recurring": "daily"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.date() == now.date()

    def test_daily_past_today(self):
        """If the time already passed today, fires tomorrow."""
        now = datetime.now(LOCAL_TZ)
        past_time = (now - timedelta(hours=2)).strftime("%H:%M")
        reminder = {"time": past_time, "recurring": "daily"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.date() == (now + timedelta(days=1)).date()

    def test_weekly_same_day_future(self):
        """If today is the target day and time hasn't passed, fires today."""
        now = datetime.now(LOCAL_TZ)
        day_name = now.strftime("%A").lower()
        future_time = (now + timedelta(hours=2)).strftime("%H:%M")
        reminder = {"time": future_time, "day": day_name, "recurring": "weekly"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.date() == now.date()

    def test_weekly_same_day_past(self):
        """If today is the target day but time passed, fires next week."""
        now = datetime.now(LOCAL_TZ)
        day_name = now.strftime("%A").lower()
        past_time = (now - timedelta(hours=2)).strftime("%H:%M")
        reminder = {"time": past_time, "day": day_name, "recurring": "weekly"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.date() == (now + timedelta(weeks=1)).date()

    def test_weekly_different_day(self):
        """Fires on the next occurrence of the target day."""
        now = datetime.now(LOCAL_TZ)
        # Pick a different day
        target_weekday = (now.weekday() + 3) % 7
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        day_name = day_names[target_weekday]
        reminder = {"time": "09:00", "day": day_name, "recurring": "weekly"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.weekday() == target_weekday

    def test_weekly_invalid_day(self):
        reminder = {"time": "09:00", "day": "moonday", "recurring": "weekly"}
        result = compute_next_fire(reminder)
        assert result is None

    def test_weekdays_on_weekday(self):
        """On a weekday, if time hasn't passed, fires today."""
        # Use a known Monday
        monday = datetime(2026, 4, 27, 6, 0, 0, tzinfo=LOCAL_TZ)  # Monday 6am
        reminder = {"time": "09:00", "recurring": "weekdays"}
        result = compute_next_fire(reminder, after=monday)
        assert result is not None
        assert result.weekday() == 0  # Monday
        assert result.hour == 9

    def test_weekdays_skips_weekend(self):
        """On Friday past the time, should skip to Monday."""
        friday_evening = datetime(2026, 5, 1, 20, 0, 0, tzinfo=LOCAL_TZ)  # Friday 8pm
        reminder = {"time": "09:00", "recurring": "weekdays"}
        result = compute_next_fire(reminder, after=friday_evening)
        assert result is not None
        assert result.weekday() == 0  # Monday
        assert result.day == 4  # May 4, 2026

    def test_weekdays_on_saturday(self):
        """On Saturday, should skip to Monday."""
        saturday = datetime(2026, 5, 2, 10, 0, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "09:00", "recurring": "weekdays"}
        result = compute_next_fire(reminder, after=saturday)
        assert result is not None
        assert result.weekday() == 0  # Monday

    def test_monthly_day_1(self):
        """Monthly on day 1."""
        march_15 = datetime(2026, 3, 15, 10, 0, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "07:00", "day": "1", "recurring": "monthly"}
        result = compute_next_fire(reminder, after=march_15)
        assert result is not None
        assert result.month == 4
        assert result.day == 1

    def test_monthly_day_15_before(self):
        """Monthly on day 15, before the 15th."""
        march_10 = datetime(2026, 3, 10, 10, 0, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "07:00", "day": "15", "recurring": "monthly"}
        result = compute_next_fire(reminder, after=march_10)
        assert result is not None
        assert result.month == 3
        assert result.day == 15

    def test_monthly_last_day(self):
        """Monthly on 'last' day."""
        march_15 = datetime(2026, 3, 15, 10, 0, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "07:00", "day": "last", "recurring": "monthly"}
        result = compute_next_fire(reminder, after=march_15)
        assert result is not None
        assert result.month == 3
        assert result.day == 31  # Last day of March

    def test_monthly_last_day_past(self):
        """Monthly on 'last' day, when already past this month's last day."""
        march_31_evening = datetime(2026, 3, 31, 20, 0, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "07:00", "day": "last", "recurring": "monthly"}
        result = compute_next_fire(reminder, after=march_31_evening)
        assert result is not None
        assert result.month == 4
        assert result.day == 30  # Last day of April

    def test_monthly_day_31_in_short_month(self):
        """Monthly on day 31 in a month with fewer days should clamp."""
        april_1 = datetime(2026, 4, 1, 10, 0, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "07:00", "day": "31", "recurring": "monthly"}
        result = compute_next_fire(reminder, after=april_1)
        assert result is not None
        assert result.month == 4
        assert result.day == 30  # April has 30 days

    def test_invalid_recurring_type(self):
        reminder = {"time": "07:00", "recurring": "biweekly"}
        result = compute_next_fire(reminder)
        assert result is None

    def test_invalid_time_format(self):
        reminder = {"time": "seven", "recurring": "daily"}
        result = compute_next_fire(reminder)
        assert result is None


# ── Delivery ─────────────────────────────────────────────────────────


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
            mock_send.assert_called_once_with(12345, "Hello!", reply_markup=None)
            assert result is True

    @patch("services.ingestion.core.scheduler.config")
    def test_send_telegram_no_user_ids(self, mock_config, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = []
        result = scheduler._send_telegram("Hello!")
        assert result is None

    @patch("services.ingestion.core.scheduler.config")
    def test_send_email(self, mock_config, scheduler):
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        with patch(
            "services.ingestion.channels.email.reply.send_reply"
        ) as mock_send:
            mock_send.return_value = True
            result = scheduler._send_email("Hello!", subject="Reminder: Hello!", session_id="test-session-123")
            mock_send.assert_called_once_with(
                to_addr="user@example.com",
                subject="Reminder: Hello!",
                body="Hello!",
                message_id="<test-session-123@synapse.local>",
            )
            assert result is True

    @patch("services.ingestion.core.scheduler.config")
    def test_send_email_no_address(self, mock_config, scheduler):
        mock_config.REPLY_TO_ADDRESS = ""
        mock_config.ALLOWED_SENDERS = []
        result = scheduler._send_email("Hello!", subject="Reminder: Hello!")
        assert result is False

    def test_deliver_unknown_channel(self, scheduler):
        result = scheduler._deliver("sms", "Hello!")
        assert result is False


# ── Message Reminder Handling ────────────────────────────────────────


class TestMessageReminder:
    """Tests for _handle_message_reminder."""

    @patch.object(ReminderScheduler, "_deliver")
    def test_message_telegram_normal(self, mock_deliver, scheduler):
        mock_deliver.return_value = True
        scheduler._handle_message_reminder("telegram", "Call the dentist")
        mock_deliver.assert_called_once()
        text = mock_deliver.call_args.args[1]
        assert "Reminder: Call the dentist" in text
        assert "Missed" not in text

    @patch.object(ReminderScheduler, "_deliver")
    def test_message_telegram_missed(self, mock_deliver, scheduler):
        mock_deliver.return_value = True
        scheduler._handle_message_reminder("telegram", "Call the dentist", is_missed=True)
        mock_deliver.assert_called_once()
        text = mock_deliver.call_args.args[1]
        assert "⏰ Missed reminder:" in text

    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch.object(ReminderScheduler, "_deliver")
    def test_message_delivery_failure(self, mock_deliver, mock_fallback, scheduler):
        mock_deliver.return_value = False
        scheduler._handle_message_reminder("telegram", "Call the dentist")
        mock_fallback.assert_called_once_with("Call the dentist")


# ── Work Reminder Handling ───────────────────────────────────────────


class TestWorkReminder:
    """Tests for _handle_work_reminder."""

    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
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
        assert "Here are the stock results..." in call_args.args[1]

    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_pipe_error(self, mock_config, mock_pipe, mock_fallback, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        mock_pipe.return_value = MagicMock(
            is_error=True,
            output="API error",
        )

        scheduler._handle_work_reminder("telegram", "Research stocks")

        mock_fallback.assert_called_once_with("Research stocks")

    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_missed_prefix(self, mock_config, mock_pipe, mock_deliver, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        mock_pipe.return_value = MagicMock(
            is_error=False,
            output="Stock results",
        )
        mock_deliver.return_value = True

        scheduler._handle_work_reminder("telegram", "Research stocks", is_missed=True)

        text = mock_deliver.call_args.args[1]
        assert "⏰" in text
        assert "Missed" in text


# ── Delivery Failure ─────────────────────────────────────────────────


class TestDeliveryFailure:
    """Tests for _handle_delivery_failure."""

    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_fallback_logs_to_master_todos(self, mock_pipe, scheduler):
        mock_pipe.return_value = MagicMock(is_error=False, output="")

        scheduler._handle_delivery_failure("Call the dentist")

        mock_pipe.assert_called_once()
        prompt_arg = mock_pipe.call_args[0][0]
        assert "master_todos" in prompt_arg
        assert "Call the dentist" in prompt_arg

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_fallback_handles_exception(self, mock_pipe, mock_email, scheduler):
        mock_pipe.side_effect = Exception("Total failure")
        # Should not raise
        scheduler._handle_delivery_failure("Call the dentist")

        mock_email.assert_called_once()
        kwargs = mock_email.call_args[1]
        assert "Call the dentist" in kwargs["text"]
        assert "Total failure" in kwargs["text"]
        assert "execute and log" in kwargs["subject"]

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_fallback_returns_error_sends_email(self, mock_pipe, mock_email, scheduler):
        mock_pipe.return_value = MagicMock(is_error=True, output="API error")

        scheduler._handle_delivery_failure("Call the dentist")

        mock_email.assert_called_once()
        kwargs = mock_email.call_args[1]
        assert "Call the dentist" in kwargs["text"]
        assert "API error" in kwargs["text"]
        assert "execute and log" in kwargs["subject"]


# ── Fire Reminder ────────────────────────────────────────────────────


class TestFireReminder:
    """Tests for _fire_reminder."""

    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_message_reminder(self, mock_handle, scheduler):
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-1", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "daily", "time": "07:00"}
        scheduler._fire_reminder(reminder, now)
        mock_handle.assert_called_once_with("telegram", "Buy milk", is_missed=False)

    @patch.object(ReminderScheduler, "_handle_work_reminder")
    def test_fire_work_reminder(self, mock_handle, scheduler):
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-2", "type": "work", "channel": "email", "task": "Research stocks", "recurring": "daily", "time": "07:00"}
        scheduler._fire_reminder(reminder, now)
        mock_handle.assert_called_once_with("email", "Research stocks", is_missed=False)

    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_missed_reminder(self, mock_handle, scheduler):
        """Reminder more than MISSED_THRESHOLD_SECONDS late gets missed prefix."""
        old_time = datetime.now(LOCAL_TZ) - timedelta(seconds=MISSED_THRESHOLD_SECONDS + 60)
        reminder = {"id": "test-3", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "daily", "time": "07:00"}
        scheduler._fire_reminder(reminder, old_time)
        mock_handle.assert_called_once_with("telegram", "Buy milk", is_missed=True)

    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_slightly_late_not_missed(self, mock_handle, scheduler):
        """Reminder less than MISSED_THRESHOLD_SECONDS late is NOT missed."""
        slightly_old = datetime.now(LOCAL_TZ) - timedelta(seconds=10)
        reminder = {"id": "test-4", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "daily", "time": "07:00"}
        scheduler._fire_reminder(reminder, slightly_old)
        mock_handle.assert_called_once_with("telegram", "Buy milk", is_missed=False)

    @patch.object(ReminderScheduler, "_remove_from_json")
    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_one_shot_removes_from_json(self, mock_handle, mock_remove, scheduler):
        mock_remove.return_value = True
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-5", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "none", "time": "2099-01-01T07:00:00"}
        scheduler._fire_reminder(reminder, now)
        mock_remove.assert_called_once_with("test-5")

    @patch.object(ReminderScheduler, "_send_email")
    @patch.object(ReminderScheduler, "_remove_from_json")
    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_one_shot_removal_failure_sends_email(self, mock_handle, mock_remove, mock_email, scheduler):
        mock_remove.return_value = False
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-6", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "none", "time": "2099-01-01T07:00:00"}
        scheduler._fire_reminder(reminder, now)
        mock_email.assert_called_once()
        kwargs = mock_email.call_args[1]
        assert "test-6" in kwargs["text"]
        assert "clean up" in kwargs["subject"].lower()


# ── Scan and Schedule ────────────────────────────────────────────────


class TestScanAndSchedule:
    """Tests for _scan_and_schedule."""

    @patch.object(ReminderScheduler, "_read_reminders_json")
    def test_scan_adds_new_reminders(self, mock_read, scheduler):
        mock_read.return_value = [
            {"id": "r1", "type": "message", "channel": "telegram", "task": "Test", "time": "07:00", "recurring": "daily"},
        ]
        scheduler._scan_and_schedule()
        assert "r1" in scheduler._scheduled_ids
        assert len(scheduler._heap) == 1

    @patch.object(ReminderScheduler, "_read_reminders_json")
    def test_scan_removes_deleted_reminders(self, mock_read, scheduler):
        # First scan: add a reminder
        mock_read.return_value = [
            {"id": "r1", "type": "message", "channel": "telegram", "task": "Test", "time": "07:00", "recurring": "daily"},
        ]
        scheduler._scan_and_schedule()
        assert "r1" in scheduler._scheduled_ids

        # Second scan: reminder is gone
        mock_read.return_value = []
        scheduler._scan_and_schedule()
        assert "r1" not in scheduler._scheduled_ids
        assert len(scheduler._heap) == 0

    @patch.object(ReminderScheduler, "_read_reminders_json")
    def test_scan_ignores_invalid_reminders(self, mock_read, scheduler):
        mock_read.return_value = [
            {"id": "r1", "type": "message", "channel": "telegram", "task": "Test", "time": "invalid", "recurring": "daily"},
        ]
        scheduler._scan_and_schedule()
        assert "r1" not in scheduler._scheduled_ids
        assert len(scheduler._heap) == 0

    @patch.object(ReminderScheduler, "_read_reminders_json")
    def test_scan_skips_reminders_without_id(self, mock_read, scheduler):
        mock_read.return_value = [
            {"type": "message", "channel": "telegram", "task": "Test", "time": "07:00", "recurring": "daily"},
        ]
        scheduler._scan_and_schedule()
        assert len(scheduler._heap) == 0
