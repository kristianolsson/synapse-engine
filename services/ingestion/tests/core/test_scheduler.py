"""
Tests for the ReminderScheduler core logic.

Verifies compute_next_fire, scan/schedule, dispatch, delivery, and
failure handling for the refactored two-tier scheduler.
"""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, mock_open

import pytest

from services.ingestion.core.scheduler import (
    ReminderScheduler,
    compute_next_fire,
    is_quota_error,
    parse_quota_reset,
    MISSED_THRESHOLD_SECONDS,
    QUOTA_MAX_DEFERRALS,
    QUOTA_RETRY_BUFFER,
    LOCAL_TZ,
)

# LOCAL_TZ now follows the test machine's actual system timezone (see
# config.py) rather than a hardcoded Los Angeles — imported from scheduler,
# not redefined, so test fixtures and the code under test always agree
# regardless of what timezone the test happens to run in. These tests
# exercise wall-clock/day-of-week arithmetic, which is correct for any
# zone as long as fixtures and assertions use the same one.


@pytest.fixture(autouse=True)
def no_real_git_pull():
    """sync_and_build_prompt() runs a real `git pull` against VAULT_PATH —
    never let a unit test touch the real vault."""
    with patch("services.ingestion.core.pipe._sync_git", return_value="Git Context: OK"):
        yield


@pytest.fixture(autouse=True)
def no_real_outbound_messages():
    """Never send real email/Telegram from a unit test — config loads the
    real .env, so an unmocked fallback path would message the actual user.
    Tests that exercise these functions patch them again themselves."""
    with patch("services.ingestion.channels.email.reply.send_reply", return_value=False), \
         patch("services.ingestion.channels.telegram.sender.send_telegram_messages", return_value=[]):
        yield


@pytest.fixture
def scheduler():
    """Create a scheduler instance for testing."""
    return ReminderScheduler(session_manager=MagicMock())


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
        # Anchored to noon (not live now()) so the +/-2h offset below never
        # crosses a midnight boundary — it did, intermittently, when this
        # ran within 2 hours of midnight, turning "future_time" into a
        # time-of-day string that reads as already-past relative to `now`.
        now = datetime.now(LOCAL_TZ).replace(hour=12, minute=0, second=0, microsecond=0)
        future_time = (now + timedelta(hours=2)).strftime("%H:%M")
        reminder = {"time": future_time, "recurring": "daily"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.date() == now.date()

    def test_daily_past_today(self):
        """If the time already passed today, fires tomorrow."""
        now = datetime.now(LOCAL_TZ).replace(hour=12, minute=0, second=0, microsecond=0)
        past_time = (now - timedelta(hours=2)).strftime("%H:%M")
        reminder = {"time": past_time, "recurring": "daily"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.date() == (now + timedelta(days=1)).date()

    def test_weekly_same_day_future(self):
        """If today is the target day and time hasn't passed, fires today."""
        now = datetime.now(LOCAL_TZ).replace(hour=12, minute=0, second=0, microsecond=0)
        day_name = now.strftime("%A").lower()
        future_time = (now + timedelta(hours=2)).strftime("%H:%M")
        reminder = {"time": future_time, "day": day_name, "recurring": "weekly"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result.date() == now.date()

    def test_weekly_same_day_past(self):
        """If today is the target day but time passed, fires next week."""
        now = datetime.now(LOCAL_TZ).replace(hour=12, minute=0, second=0, microsecond=0)
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
        reminder = {"time": "not-a-time", "recurring": "daily"}
        assert compute_next_fire(reminder) is None

    def test_hourly_future_minute(self):
        now = datetime(2026, 4, 27, 10, 15, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "00:30", "recurring": "hourly"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result == datetime(2026, 4, 27, 10, 30, 0, tzinfo=LOCAL_TZ)

    def test_hourly_past_minute(self):
        now = datetime(2026, 4, 27, 10, 45, 0, tzinfo=LOCAL_TZ)
        reminder = {"time": "00:30", "recurring": "hourly"}
        result = compute_next_fire(reminder, after=now)
        assert result is not None
        assert result == datetime(2026, 4, 27, 11, 30, 0, tzinfo=LOCAL_TZ)


# ── Delivery ─────────────────────────────────────────────────────────


class TestDelivery:
    """Tests for _deliver, _send_telegram, _send_email."""

    @patch("services.ingestion.core.scheduler.config")
    def test_send_telegram(self, mock_config, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        with patch(
            "services.ingestion.channels.telegram.sender.send_telegram_messages"
        ) as mock_send:
            mock_send.return_value = [101]
            result = scheduler._send_telegram("Hello!")
            mock_send.assert_called_once_with(12345, "Hello!", reply_markup=None)
            assert result == [101]

    @patch("services.ingestion.core.scheduler.config")
    def test_send_telegram_no_user_ids(self, mock_config, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = []
        result = scheduler._send_telegram("Hello!")
        assert result == []

    @patch("services.ingestion.core.scheduler.config")
    def test_send_telegram_sanitizes_html(self, mock_config, scheduler):
        # Unlike listener.py, this path used to send raw LLM output with no
        # sanitization or fallback — an unsupported tag would make Telegram
        # reject the whole reminder outright.
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.TELEGRAM_BOT_TOKEN = "test-token"

        with patch(
            "services.ingestion.channels.telegram.sender.send_telegram_messages"
        ) as mock_send:
            mock_send.return_value = [101]
            scheduler._send_telegram("<div>Reminder</div><script>bad()</script>")
            sent_text = mock_send.call_args.args[1]
            assert "<div>" not in sent_text
            assert "<script>" not in sent_text
            assert "Reminder" in sent_text

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

    @patch("services.ingestion.core.scheduler.config")
    def test_send_email_swallows_exception(self, mock_config, scheduler):
        """send_reply (SMTP) raising must not propagate — callers rely on
        _send_email always returning, never raising."""
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        with patch(
            "services.ingestion.channels.email.reply.send_reply",
            side_effect=Exception("SMTP connection refused"),
        ):
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

    @patch.object(ReminderScheduler, "_email_fallback")
    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch.object(ReminderScheduler, "_deliver")
    def test_message_delivery_failure(self, mock_deliver, mock_fallback, mock_email, scheduler):
        """Telegram and the email fallback both fail → failure handler runs."""
        mock_deliver.return_value = False
        mock_email.return_value = False
        scheduler._handle_message_reminder("telegram", "Call the dentist")
        mock_email.assert_called_once()
        mock_fallback.assert_called_once_with(
            "Call the dentist", error="Failed to deliver message via telegram.", title=None
        )

    @patch.object(ReminderScheduler, "_email_fallback")
    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch.object(ReminderScheduler, "_deliver")
    def test_message_telegram_failure_falls_back_to_email(self, mock_deliver, mock_fallback, mock_email, scheduler):
        mock_deliver.return_value = None
        mock_email.return_value = True
        scheduler._handle_message_reminder("telegram", "Call the dentist")
        assert "Call the dentist" in mock_email.call_args.args[0]
        mock_fallback.assert_not_called()


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
        assert "scheduled_work" in prompt_arg.lower()

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

        mock_fallback.assert_called_once_with("Research stocks", error="API error", title=None)

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

    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_synapse_ok_skips_delivery(self, mock_config, mock_pipe, mock_deliver, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        mock_pipe.return_value = MagicMock(
            is_error=False,
            requires_reply=False,
            output="",
        )

        scheduler._handle_work_reminder("telegram", "Check Redfin listing")

        mock_deliver.assert_not_called()

    @patch("services.ingestion.channels.telegram.reply_dispatch.attach_form_message_id")
    @patch("services.ingestion.channels.telegram.reply_dispatch.build_reply_keyboard")
    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_telegram_form_uses_form_keyboard(
        self, mock_config, mock_pipe, mock_deliver, mock_build_reply_keyboard, mock_attach, scheduler
    ):
        """A ☐F: response must go through the shared form dispatch, not the old task-only path."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        mock_pipe.return_value = MagicMock(
            is_error=False, output="☐F:yn:protein Protein?", session_id="sess-1",
        )
        fake_keyboard = MagicMock()
        mock_build_reply_keyboard.return_value = ("Protein?", fake_keyboard, "form-abc")
        mock_deliver.return_value = 999

        scheduler._handle_work_reminder("telegram", "Fitness check-in")

        mock_build_reply_keyboard.assert_called_once()
        call_args = mock_deliver.call_args
        assert call_args.args[0] == "telegram"
        assert call_args.kwargs["reply_markup"] is fake_keyboard
        mock_attach.assert_called_once_with("form-abc", 999, "sess-1")

    @patch.object(ReminderScheduler, "_save_undelivered_result", return_value="/data/undelivered/x.html")
    @patch.object(ReminderScheduler, "_email_fallback", return_value=False)
    @patch("services.ingestion.core.form_state.delete_form")
    @patch("services.ingestion.channels.telegram.reply_dispatch.build_reply_keyboard")
    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_form_delivery_failure_cleans_up_form(
        self, mock_config, mock_pipe, mock_deliver, mock_fallback, mock_build_reply_keyboard, mock_delete_form,
        mock_email, mock_save, scheduler
    ):
        """If delivery of a form-bearing reminder fails, the form must not leak in form_state."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        mock_pipe.return_value = MagicMock(
            is_error=False, output="☐F:yn:protein Protein?", session_id=None,
        )
        mock_build_reply_keyboard.return_value = ("Protein?", MagicMock(), "form-abc")
        mock_deliver.return_value = None  # delivery failed

        scheduler._handle_work_reminder("telegram", "Fitness check-in")

        mock_email.assert_called_once()
        mock_fallback.assert_called_once_with(
            "Fitness check-in", error="Task completed but failed to deliver the response via telegram.",
            title=None, saved_path="/data/undelivered/x.html", log_todo=False,
        )
        mock_delete_form.assert_called_once_with("form-abc")

    @patch("services.ingestion.channels.telegram.reply_dispatch.build_reply_keyboard")
    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_form_bakes_subject_into_text(
        self, mock_config, mock_pipe, mock_deliver, mock_build_reply_keyboard, scheduler
    ):
        """The subject header must be baked into the text passed to build_reply_keyboard
        (so it's part of the cached form state), and NOT also passed to _deliver
        (which would otherwise double-prefix it)."""
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]

        mock_pipe.return_value = MagicMock(
            is_error=False, output="☐F:yn:protein Protein?", session_id=None,
        )
        mock_build_reply_keyboard.return_value = ("Protein?", MagicMock(), "form-abc")
        mock_deliver.return_value = 999

        scheduler._handle_work_reminder("telegram", "Fitness check-in", subject_override="Fitness Focus Check-in")

        dispatch_text = mock_build_reply_keyboard.call_args.args[2]
        assert "<b>Fitness Focus Check-in</b>" in dispatch_text
        assert mock_deliver.call_args.kwargs["subject"] == ""

    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_reminder_passes_reminder_correlation_extra_env(self, mock_config, mock_pipe, mock_deliver, scheduler):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.REPLY_TO_ADDRESS = "user@example.com"
        mock_config.ALLOWED_SENDERS = ["user@example.com"]

        mock_pipe.return_value = MagicMock(
            is_error=False,
            output="Found 2 opportunities",
            requires_reply=False,
            session_id=None,
        )
        mock_deliver.return_value = True

        scheduler._handle_work_reminder("email", "Run options-bot scan --tickers AAPL,MSFT")

        extra_env = mock_pipe.call_args.kwargs["extra_env"]
        assert extra_env == {
            "SYNAPSE_CHANNEL": "email",
            "SYNAPSE_REMINDER_TASK": "Run options-bot scan --tickers AAPL,MSFT",
            "SYNAPSE_EMAIL_TO": "user@example.com",
            "SYNAPSE_EMAIL_SUBJECT": "Synapse: Run options-bot scan --tickers AAPL,MSFT",
        }


# ── Delivery Failure ─────────────────────────────────────────────────


class TestDeliveryFailure:
    """Tests for _handle_delivery_failure."""

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_fallback_logs_to_master_todos(self, mock_pipe, mock_email, scheduler):
        mock_pipe.return_value = MagicMock(is_error=False, output="")

        scheduler._handle_delivery_failure("Call the dentist")

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_still_logs_to_master_todos_if_alert_email_raises(self, mock_pipe, mock_email, scheduler):
        """Even if _send_email somehow raises, the task must still get
        logged to master_todos — one failure must not sink the other."""
        mock_email.side_effect = Exception("unexpected email failure")
        mock_pipe.return_value = MagicMock(is_error=False, output="")

        # Must not raise
        scheduler._handle_delivery_failure("Call the dentist")

        mock_pipe.assert_called_once()
        prompt_arg = mock_pipe.call_args[0][0]
        assert "master_todos" in prompt_arg
        assert "Call the dentist" in prompt_arg
        # Wrapped in the metadata block like every other provider prompt
        assert prompt_arg.startswith("---\nType: scheduled_work")

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_alerts_immediately_with_the_original_error(self, mock_pipe, mock_email, scheduler):
        """The user should be alerted right away for ANY failure reason, not
        just when the master_todos fallback logging also fails."""
        mock_pipe.return_value = MagicMock(is_error=False, output="")

        scheduler._handle_delivery_failure("Call the dentist", error="Claude CLI timed out after 900 seconds.")

        mock_email.assert_called_once()
        kwargs = mock_email.call_args[1]
        assert "Call the dentist" in kwargs["text"]
        assert "Claude CLI timed out after 900 seconds." in kwargs["text"]
        assert kwargs["subject"] == "[Synapse Alert] Scheduled task failed"

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_fallback_handles_exception(self, mock_pipe, mock_email, scheduler):
        mock_pipe.side_effect = Exception("Total failure")
        # Should not raise
        scheduler._handle_delivery_failure("Call the dentist")

        # First call: immediate alert. Second call: logging fallback also failed.
        assert mock_email.call_count == 2
        first_kwargs = mock_email.call_args_list[0][1]
        assert first_kwargs["subject"] == "[Synapse Alert] Scheduled task failed"

        second_kwargs = mock_email.call_args_list[1][1]
        assert "Call the dentist" in second_kwargs["text"]
        assert "Total failure" in second_kwargs["text"]
        assert "execute and log" in second_kwargs["subject"]

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_fallback_returns_error_sends_email(self, mock_pipe, mock_email, scheduler):
        mock_pipe.return_value = MagicMock(is_error=True, output="API error")

        scheduler._handle_delivery_failure("Call the dentist")

        # First call: immediate alert. Second call: logging fallback also failed.
        assert mock_email.call_count == 2
        second_kwargs = mock_email.call_args_list[1][1]
        assert "Call the dentist" in second_kwargs["text"]
        assert "API error" in second_kwargs["text"]
        assert "execute and log" in second_kwargs["subject"]


    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_todo_uses_short_title_not_full_prompt(self, mock_pipe, mock_email, scheduler):
        mock_pipe.return_value = MagicMock(is_error=False, output="")
        long_task = "Check if there is a F1 race this weekend. " + "Lots of instructions. " * 50

        scheduler._handle_delivery_failure(long_task, title="F1 weekly")

        prompt_arg = mock_pipe.call_args[0][0]
        assert "- [ ] Scheduled task failed (" in prompt_arg
        assert prompt_arg.rstrip().endswith("): F1 weekly")
        assert "Lots of instructions." not in prompt_arg

    @patch.object(ReminderScheduler, "_send_email")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    def test_log_todo_false_skips_ai_logging(self, mock_pipe, mock_email, scheduler):
        scheduler._handle_delivery_failure("Call the dentist", log_todo=False)
        mock_email.assert_called_once()
        mock_pipe.assert_not_called()


class TestTelegramEmailFallback:
    """Work reminders fall back to email when Telegram delivery fails."""

    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch.object(ReminderScheduler, "_send_email", return_value=True)
    @patch.object(ReminderScheduler, "_deliver", return_value=None)
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_work_result_emailed_when_telegram_fails(
        self, mock_config, mock_pipe, mock_deliver, mock_send_email, mock_failure, scheduler
    ):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_pipe.return_value = MagicMock(
            is_error=False, requires_reply=True, output="F1 weekend report", session_id="sess-9", stats=None,
        )

        scheduler._handle_work_reminder("telegram", "F1 report", subject_override="F1 weekly")

        mock_send_email.assert_called_once()
        assert "F1 weekend report" in mock_send_email.call_args.args[0]
        assert mock_send_email.call_args.kwargs["subject"] == "F1 weekly"
        assert mock_send_email.call_args.kwargs["session_id"] == "sess-9"
        mock_failure.assert_not_called()

    @patch("services.ingestion.core.scheduler.config")
    def test_save_undelivered_result(self, mock_config, scheduler, tmp_path):
        mock_config.UNDELIVERED_RESULTS_DIR = str(tmp_path / "undelivered")
        path = scheduler._save_undelivered_result(
            "F1 report", "<b>Result</b>", channel="telegram", subject="F1 weekly", session_id="s1"
        )
        assert path.startswith(str(tmp_path))
        with open(path) as f:
            record = json.load(f)
        assert record["text"] == "<b>Result</b>"
        assert record["channel"] == "telegram"
        assert record["session_id"] == "s1"


class TestResendUndelivered:
    """Saved results are re-sent on the hourly scan and deleted once sent."""

    def _save(self, scheduler, tmp_path, mock_config, **kw):
        mock_config.UNDELIVERED_RESULTS_DIR = str(tmp_path)
        return scheduler._save_undelivered_result(
            "F1 report", "<b>Result</b>", channel="telegram", subject="F1 weekly",
            email_subject="F1 weekly", session_id="s1", **kw,
        )

    @patch.object(ReminderScheduler, "_deliver", return_value=42)
    @patch("services.ingestion.core.scheduler.config")
    def test_resend_success_deletes_file(self, mock_config, mock_deliver, scheduler, tmp_path):
        path = self._save(scheduler, tmp_path, mock_config)
        scheduler._resend_undelivered()
        assert mock_deliver.call_args.args[0] == "telegram"
        assert "<b>Result</b>" in mock_deliver.call_args.args[1]
        assert "Delayed delivery" in mock_deliver.call_args.args[1]
        assert not os.path.exists(path)

    @patch.object(ReminderScheduler, "_email_fallback", return_value=False)
    @patch.object(ReminderScheduler, "_deliver", return_value=None)
    @patch("services.ingestion.core.scheduler.config")
    def test_resend_failure_keeps_file(self, mock_config, mock_deliver, mock_email, scheduler, tmp_path):
        path = self._save(scheduler, tmp_path, mock_config)
        scheduler._resend_undelivered()
        mock_email.assert_called_once()
        assert os.path.exists(path)

    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch.object(ReminderScheduler, "_deliver")
    @patch("services.ingestion.core.scheduler.config")
    def test_expired_result_alerts_and_deletes(self, mock_config, mock_deliver, mock_failure, scheduler, tmp_path):
        path = self._save(scheduler, tmp_path, mock_config)
        with open(path) as f:
            record = json.load(f)
        record["saved_at"] = (datetime.now(LOCAL_TZ) - timedelta(days=31)).isoformat()
        with open(path, "w") as f:
            json.dump(record, f)

        scheduler._resend_undelivered()

        mock_deliver.assert_not_called()
        mock_failure.assert_called_once()
        assert mock_failure.call_args.kwargs["title"] == "F1 weekly"
        assert not os.path.exists(path)

    def test_deliver_maps_every_split_part_to_the_session(self, scheduler):
        with patch.object(ReminderScheduler, "_send_telegram", return_value=[1, 2, 3]):
            result = scheduler._deliver("telegram", "long text", session_id="sess-1")
        assert result == 3
        saved = [c.args for c in scheduler.session_manager.save_message_session.call_args_list]
        assert saved == [(1, "sess-1"), (2, "sess-1"), (3, "sess-1")]


class TestQuotaHandling:
    """Usage-limit errors defer scheduled work instead of failing it."""

    def test_is_quota_error(self):
        assert is_quota_error("Quota limit reached: You've hit your session limit · resets 8:30pm")
        assert not is_quota_error("Claude CLI timed out")

    def test_parse_reset_later_today(self):
        now = datetime(2026, 10, 30, 18, 0, tzinfo=LOCAL_TZ)
        reset = parse_quota_reset("You've hit your session limit · resets 8:30pm", now)
        assert reset == datetime(2026, 10, 30, 20, 30, tzinfo=LOCAL_TZ)

    def test_parse_reset_rolls_to_tomorrow(self):
        now = datetime(2026, 10, 30, 23, 0, tzinfo=LOCAL_TZ)
        reset = parse_quota_reset("resets 2am", now)
        assert reset == datetime(2026, 10, 31, 2, 0, tzinfo=LOCAL_TZ)

    def test_parse_reset_unparseable(self):
        now = datetime(2026, 10, 30, 18, 0, tzinfo=LOCAL_TZ)
        assert parse_quota_reset("Quota limit reached", now) is None

    @patch("services.ingestion.core.scheduler.threading.Timer")
    @patch.object(ReminderScheduler, "_notify")
    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    @patch("services.ingestion.core.scheduler.pipe_to_provider")
    @patch("services.ingestion.core.scheduler.config")
    def test_quota_error_defers_with_one_alert_and_no_failure_log(
        self, mock_config, mock_pipe, mock_failure, mock_notify, mock_timer, scheduler
    ):
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [12345]
        mock_config.get_next_provider.return_value = None
        mock_pipe.return_value = MagicMock(
            is_error=True, output="Quota limit reached: You've hit your session limit · resets 8:30pm",
        )

        scheduler._handle_work_reminder("email", "Scan inbox")
        # A second task firing while blocked must not start a session or re-alert.
        scheduler._handle_work_reminder("email", "Scan links")

        assert mock_pipe.call_count == 1
        mock_notify.assert_called_once()
        mock_failure.assert_not_called()
        assert mock_timer.call_count == 2
        assert scheduler._quota_blocked_until is not None
        assert scheduler._quota_blocked_until.minute == (30 + QUOTA_RETRY_BUFFER.seconds // 60) % 60

    @patch("services.ingestion.core.scheduler.threading.Timer")
    @patch.object(ReminderScheduler, "_handle_delivery_failure")
    def test_gives_up_after_max_deferrals(self, mock_failure, mock_timer, scheduler):
        scheduler._defer_for_quota(
            "email", "Scan inbox", None, datetime.now(LOCAL_TZ), QUOTA_MAX_DEFERRALS, error="resets 8pm",
        )
        mock_timer.assert_not_called()
        assert mock_failure.call_args.kwargs["log_todo"] is False


# ── Fire Reminder ────────────────────────────────────────────────────


class TestFireReminder:
    """Tests for _fire_reminder."""

    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_message_reminder(self, mock_handle, scheduler):
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-1", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "daily", "time": "07:00"}
        scheduler._fire_reminder(reminder, now)
        mock_handle.assert_called_once_with("telegram", "Buy milk", is_missed=False, subject_override=None)

    @patch.object(ReminderScheduler, "_handle_work_reminder")
    def test_fire_work_reminder(self, mock_handle, scheduler):
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-2", "type": "work", "channel": "email", "task": "Research stocks", "recurring": "none", "time": "2099-01-01T07:00:00"}
        scheduler._fire_reminder(reminder, now)
        mock_handle.assert_called_once()
        args, kwargs = mock_handle.call_args
        assert args == ("email", "Research stocks")
        assert kwargs["is_missed"] is False
        assert "seconds_late" in kwargs

    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_missed_reminder(self, mock_handle, scheduler):
        """Reminder more than MISSED_THRESHOLD_SECONDS late gets missed prefix."""
        old_time = datetime.now(LOCAL_TZ) - timedelta(seconds=MISSED_THRESHOLD_SECONDS + 60)
        reminder = {"id": "test-3", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "daily", "time": "07:00"}
        scheduler._fire_reminder(reminder, old_time)
        mock_handle.assert_called_once_with("telegram", "Buy milk", is_missed=True, subject_override=None)

    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_slightly_late_not_missed(self, mock_handle, scheduler):
        """Reminder less than MISSED_THRESHOLD_SECONDS late is NOT missed."""
        slightly_old = datetime.now(LOCAL_TZ) - timedelta(seconds=10)
        reminder = {"id": "test-4", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "daily", "time": "07:00"}
        scheduler._fire_reminder(reminder, slightly_old)
        mock_handle.assert_called_once_with("telegram", "Buy milk", is_missed=False, subject_override=None)

    @patch.object(ReminderScheduler, "_remove_from_json")
    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_one_shot_removes_from_json(self, mock_handle, mock_remove, scheduler):
        mock_remove.return_value = True
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-5", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "none", "time": "2099-01-01T07:00:00"}
        scheduler._fire_reminder(reminder, now)
        mock_remove.assert_called_once_with("test-5")

    @patch("services.ingestion.core.scheduler.logger")
    @patch.object(ReminderScheduler, "_remove_from_json")
    @patch.object(ReminderScheduler, "_handle_message_reminder")
    def test_fire_one_shot_removal_failure_logs_warning(self, mock_handle, mock_remove, mock_logger, scheduler):
        mock_remove.return_value = False
        now = datetime.now(LOCAL_TZ)
        reminder = {"id": "test-6", "type": "message", "channel": "telegram", "task": "Buy milk", "recurring": "none", "time": "2099-01-01T07:00:00"}
        scheduler._fire_reminder(reminder, now)
        mock_logger.warning.assert_called_once()
        assert "not found in reminders.json" in mock_logger.warning.call_args[0][0]


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
