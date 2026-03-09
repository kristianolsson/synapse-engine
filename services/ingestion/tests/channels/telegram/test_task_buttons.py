"""Tests for task_buttons utility module."""

import pytest
from unittest.mock import MagicMock

from services.ingestion.channels.telegram.task_buttons import (
    parse_tasks,
    build_task_keyboard,
    recover_task_from_callback,
    _hash_task,
)


class TestParseTasks:
    def test_single_task(self):
        text = "Here are your tasks:\n☐ Buy groceries\nHave a great day!"
        tasks = parse_tasks(text)
        assert len(tasks) == 1
        assert tasks[0]["text"] == "Buy groceries"
        assert tasks[0]["hash"] == _hash_task("Buy groceries")

    def test_multiple_tasks(self):
        text = "☐ Buy groceries\n☐ Fix the fence\n☐ Call dentist"
        tasks = parse_tasks(text)
        assert len(tasks) == 3
        assert [t["text"] for t in tasks] == ["Buy groceries", "Fix the fence", "Call dentist"]

    def test_no_tasks(self):
        text = "No actionable items today. Just a regular message."
        tasks = parse_tasks(text)
        assert len(tasks) == 0

    def test_mixed_content(self):
        text = "Your top todos:\n☐ Buy groceries\nSome info here\n☐ Fix the fence\nMore info"
        tasks = parse_tasks(text)
        assert len(tasks) == 2

    def test_empty_string(self):
        tasks = parse_tasks("")
        assert len(tasks) == 0

    def test_checkbox_without_text(self):
        """Lines with ☐ but no meaningful text should be skipped."""
        text = "☐ \n☐ Real task"
        tasks = parse_tasks(text)
        # Only the second line should match
        assert len(tasks) == 1
        assert "Real task" in tasks[0]["text"]

    def test_completed_tasks_not_matched(self):
        """Completed tasks (✅) should not be parsed."""
        text = "☐ Open task\n✅ Done task"
        tasks = parse_tasks(text)
        assert len(tasks) == 1
        assert tasks[0]["text"] == "Open task"

    def test_unique_hashes(self):
        text = "☐ Task A\n☐ Task B"
        tasks = parse_tasks(text)
        assert tasks[0]["hash"] != tasks[1]["hash"]


class TestBuildTaskKeyboard:
    def test_returns_none_for_empty(self):
        assert build_task_keyboard([]) is None

    def test_builds_keyboard(self):
        tasks = [
            {"text": "Buy groceries", "hash": "abc12345"},
            {"text": "Fix the fence", "hash": "def67890"},
        ]
        keyboard = build_task_keyboard(tasks)
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 2
        assert keyboard.inline_keyboard[0][0].callback_data == "done_abc12345"
        assert keyboard.inline_keyboard[1][0].callback_data == "done_def67890"

    def test_truncates_long_labels(self):
        tasks = [{"text": "A" * 60, "hash": "abc12345"}]
        keyboard = build_task_keyboard(tasks)
        label = keyboard.inline_keyboard[0][0].text
        # Label = "✅ " + truncated text, so check it's not the full 60 chars
        assert len(label) < 60

    def test_button_labels_start_with_checkmark(self):
        tasks = [{"text": "Buy groceries", "hash": "abc12345"}]
        keyboard = build_task_keyboard(tasks)
        assert keyboard.inline_keyboard[0][0].text.startswith("✅")


class TestRecoverTaskFromCallback:
    def test_recovers_task(self):
        text = "☐ Buy groceries\n☐ Fix the fence"
        task_hash = _hash_task("Buy groceries")
        result = recover_task_from_callback(text, task_hash)
        assert result == "Buy groceries"

    def test_returns_none_for_unknown_hash(self):
        text = "☐ Buy groceries"
        result = recover_task_from_callback(text, "deadbeef")
        assert result is None

    def test_returns_none_for_empty_text(self):
        result = recover_task_from_callback("", "abc12345")
        assert result is None

    def test_works_with_mixed_completed(self):
        """Should recover tasks even when some are already completed."""
        text = "✅ Done task\n☐ Open task"
        task_hash = _hash_task("Open task")
        result = recover_task_from_callback(text, task_hash)
        assert result == "Open task"
