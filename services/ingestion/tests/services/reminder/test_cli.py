"""
Tests for the reminder CLI tool.

Verifies add, edit, remove, list commands, validation, and file locking.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from services.ingestion.services.reminder.cli import (
    cmd_add,
    cmd_edit,
    cmd_remove,
    cmd_list,
    _read_reminders,
    _write_reminders,
    _validate_time_format,
    _validate_day,
    main,
)


@pytest.fixture
def tmp_reminders(tmp_path):
    """Create a temporary reminders.json file."""
    path = tmp_path / "reminders.json"
    path.write_text("[]")
    return path


@pytest.fixture
def sample_reminders(tmp_path):
    """Create a temporary reminders.json with sample data."""
    path = tmp_path / "reminders.json"
    data = [
        {
            "id": "TEST-ID-1",
            "type": "work",
            "channel": "telegram",
            "task": "Research stocks",
            "time": "07:00",
            "recurring": "daily",
            "created_at": "2026-04-27T00:00:00",
        },
        {
            "id": "TEST-ID-2",
            "type": "message",
            "channel": "email",
            "task": "Buy TV",
            "time": "2026-06-22T07:00:00",
            "recurring": "none",
            "created_at": "2026-04-12T00:00:00",
        },
    ]
    path.write_text(json.dumps(data, indent=2))
    return path


class _MockArgs:
    """Helper to create mock argparse Namespace-like objects."""

    def __init__(self, **kwargs):
        # Mirror argparse defaults for optional flags the tests don't set,
        # so command handlers can read them unconditionally. None is falsy
        # for cmd_add's `if args.subject` and skips cmd_edit's `is not None`.
        kwargs.setdefault("subject", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


# ── Validation ───────────────────────────────────────────────────────


class TestValidation:
    """Tests for time and day validation."""

    def test_valid_one_shot_time(self):
        _validate_time_format("2026-06-22T07:00:00", "none")

    def test_invalid_one_shot_time(self):
        with pytest.raises(ValueError, match="full ISO datetime"):
            _validate_time_format("07:00", "none")

    def test_valid_recurring_time(self):
        _validate_time_format("07:00", "daily")
        _validate_time_format("15:30", "weekly")
        _validate_time_format("23:59", "weekdays")

    def test_invalid_recurring_time_format(self):
        with pytest.raises(ValueError, match="HH:MM"):
            _validate_time_format("7am", "daily")

    def test_invalid_recurring_time_values(self):
        with pytest.raises(ValueError, match="valid hour"):
            _validate_time_format("25:00", "daily")

    def test_valid_weekly_day(self):
        _validate_day("friday", "weekly")
        _validate_day("Monday", "weekly")

    def test_invalid_weekly_day(self):
        with pytest.raises(ValueError, match="weekday name"):
            _validate_day("moonday", "weekly")

    def test_valid_monthly_day_number(self):
        _validate_day("1", "monthly")
        _validate_day("15", "monthly")
        _validate_day("31", "monthly")

    def test_valid_monthly_day_last(self):
        _validate_day("last", "monthly")

    def test_invalid_monthly_day(self):
        with pytest.raises(ValueError, match="day number"):
            _validate_day("32", "monthly")

    def test_invalid_monthly_day_string(self):
        with pytest.raises(ValueError, match="day number"):
            _validate_day("first", "monthly")


# ── Add Command ──────────────────────────────────────────────────────


class TestAddCommand:
    """Tests for cmd_add."""

    def test_add_daily(self, tmp_reminders):
        args = _MockArgs(
            type="work", task="Research stocks", time="07:00",
            recurring="daily", day="", channel="telegram", source=""
        )
        result = cmd_add(args, tmp_reminders)
        assert "Reminder created:" in result
        assert "daily" in result

        data = json.loads(tmp_reminders.read_text())
        assert len(data) == 1
        assert data[0]["type"] == "work"
        assert data[0]["recurring"] == "daily"
        assert "day" not in data[0]

    def test_add_weekly(self, tmp_reminders):
        args = _MockArgs(
            type="work", task="F1 check", time="15:00",
            recurring="weekly", day="friday", channel="telegram", source=""
        )
        result = cmd_add(args, tmp_reminders)
        assert "friday" in result

        data = json.loads(tmp_reminders.read_text())
        assert data[0]["day"] == "friday"

    def test_add_one_shot(self, tmp_reminders):
        args = _MockArgs(
            type="message", task="Buy TV", time="2026-06-22T07:00:00",
            recurring="none", day="", channel="email", source="daily/2026-04-12"
        )
        result = cmd_add(args, tmp_reminders)
        data = json.loads(tmp_reminders.read_text())
        assert data[0]["recurring"] == "none"
        assert data[0]["source"] == "daily/2026-04-12"

    def test_add_weekdays(self, tmp_reminders):
        args = _MockArgs(
            type="work", task="Morning standup", time="09:00",
            recurring="weekdays", day="", channel="telegram", source=""
        )
        cmd_add(args, tmp_reminders)
        data = json.loads(tmp_reminders.read_text())
        assert data[0]["recurring"] == "weekdays"

    def test_add_monthly(self, tmp_reminders):
        args = _MockArgs(
            type="message", task="Pay rent", time="09:00",
            recurring="monthly", day="1", channel="telegram", source=""
        )
        cmd_add(args, tmp_reminders)
        data = json.loads(tmp_reminders.read_text())
        assert data[0]["recurring"] == "monthly"
        assert data[0]["day"] == "1"

    def test_add_invalid_type(self, tmp_reminders):
        args = _MockArgs(
            type="invalid", task="Test", time="07:00",
            recurring="daily", day="", channel="telegram", source=""
        )
        with pytest.raises(ValueError, match="--type"):
            cmd_add(args, tmp_reminders)

    def test_add_invalid_channel(self, tmp_reminders):
        args = _MockArgs(
            type="message", task="Test", time="07:00",
            recurring="daily", day="", channel="sms", source=""
        )
        with pytest.raises(ValueError, match="--channel"):
            cmd_add(args, tmp_reminders)

    def test_add_weekly_missing_day(self, tmp_reminders):
        args = _MockArgs(
            type="work", task="Test", time="07:00",
            recurring="weekly", day="", channel="telegram", source=""
        )
        with pytest.raises(ValueError, match="--day is required"):
            cmd_add(args, tmp_reminders)

    def test_add_daily_with_day_rejects(self, tmp_reminders):
        args = _MockArgs(
            type="work", task="Test", time="07:00",
            recurring="daily", day="friday", channel="telegram", source=""
        )
        with pytest.raises(ValueError, match="not applicable"):
            cmd_add(args, tmp_reminders)

    def test_add_generates_uuid(self, tmp_reminders):
        args = _MockArgs(
            type="message", task="Test", time="07:00",
            recurring="daily", day="", channel="telegram", source=""
        )
        cmd_add(args, tmp_reminders)
        data = json.loads(tmp_reminders.read_text())
        assert len(data[0]["id"]) == 36  # UUID format


# ── Edit Command ─────────────────────────────────────────────────────


class TestEditCommand:
    """Tests for cmd_edit."""

    def test_edit_task(self, sample_reminders):
        args = _MockArgs(
            id="TEST-ID-1", type="", task="Updated task", time="",
            recurring="", day="", channel="", source=None
        )
        result = cmd_edit(args, sample_reminders)
        assert "Updated task" in result

        data = json.loads(sample_reminders.read_text())
        assert data[0]["task"] == "Updated task"

    def test_edit_time(self, sample_reminders):
        args = _MockArgs(
            id="TEST-ID-1", type="", task="", time="09:00",
            recurring="", day="", channel="", source=None
        )
        cmd_edit(args, sample_reminders)
        data = json.loads(sample_reminders.read_text())
        assert data[0]["time"] == "09:00"

    def test_edit_channel(self, sample_reminders):
        args = _MockArgs(
            id="TEST-ID-1", type="", task="", time="",
            recurring="", day="", channel="email", source=None
        )
        cmd_edit(args, sample_reminders)
        data = json.loads(sample_reminders.read_text())
        assert data[0]["channel"] == "email"

    def test_edit_not_found(self, sample_reminders):
        args = _MockArgs(
            id="NONEXISTENT", type="", task="", time="",
            recurring="", day="", channel="", source=None
        )
        with pytest.raises(ValueError, match="No reminder found"):
            cmd_edit(args, sample_reminders)


# ── Remove Command ───────────────────────────────────────────────────


class TestRemoveCommand:
    """Tests for cmd_remove."""

    def test_remove_existing(self, sample_reminders):
        args = _MockArgs(id="TEST-ID-1")
        result = cmd_remove(args, sample_reminders)
        assert "Reminder removed" in result

        data = json.loads(sample_reminders.read_text())
        assert len(data) == 1
        assert data[0]["id"] == "TEST-ID-2"

    def test_remove_not_found(self, sample_reminders):
        args = _MockArgs(id="NONEXISTENT")
        with pytest.raises(ValueError, match="No reminder found"):
            cmd_remove(args, sample_reminders)


# ── List Command ─────────────────────────────────────────────────────


class TestListCommand:
    """Tests for cmd_list."""

    def test_list_human_readable(self, sample_reminders):
        args = _MockArgs(json=False)
        result = cmd_list(args, sample_reminders)
        assert "Reminders (2)" in result
        assert "Research stocks" in result
        assert "Buy TV" in result

    def test_list_json_output(self, sample_reminders):
        args = _MockArgs(json=True)
        result = cmd_list(args, sample_reminders)
        data = json.loads(result)
        assert len(data) == 2

    def test_list_empty(self, tmp_reminders):
        args = _MockArgs(json=False)
        result = cmd_list(args, tmp_reminders)
        assert "No reminders" in result


# ── File I/O ─────────────────────────────────────────────────────────


class TestFileIO:
    """Tests for read/write with file locking."""

    def test_read_nonexistent(self, tmp_path):
        path = tmp_path / "missing.json"
        result = _read_reminders(path)
        assert result == []

    def test_write_creates_file(self, tmp_path):
        path = tmp_path / "new_dir" / "reminders.json"
        _write_reminders(path, [{"id": "test"}])
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1

    def test_atomic_write(self, tmp_path):
        """Write should not leave .tmp files on success."""
        path = tmp_path / "reminders.json"
        _write_reminders(path, [{"id": "test"}])
        assert not (tmp_path / "reminders.json.tmp").exists()
