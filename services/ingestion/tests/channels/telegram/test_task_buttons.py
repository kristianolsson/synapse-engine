"""Tests for task_buttons utility module."""


from services.ingestion.channels.telegram.task_buttons import (
    parse_tasks,
    format_message_with_tasks,
    build_task_keyboard,
    recover_task_from_callback,
    _hash_task,
)


class TestFormatMessageWithTasks:
    from services.ingestion.channels.telegram.task_buttons import format_message_with_tasks
    
    def test_single_task(self):
        text = "Here are your tasks:\n☐ Buy groceries\nHave a great day!"
        mod_text, tasks = format_message_with_tasks(text)
        assert len(tasks) == 1
        assert tasks[0]["text"] == "Buy groceries"
        assert tasks[0]["number"] == 1
        assert "[1] ☐ Buy groceries" in mod_text

    def test_multiple_tasks(self):
        text = "☐ Buy groceries\n☐ Fix the fence\n☐ Call dentist"
        mod_text, tasks = format_message_with_tasks(text)
        assert len(tasks) == 3
        assert [t["text"] for t in tasks] == ["Buy groceries", "Fix the fence", "Call dentist"]
        assert [t["number"] for t in tasks] == [1, 2, 3]
        assert "[1] ☐ Buy groceries" in mod_text
        assert "[3] ☐ Call dentist" in mod_text

    def test_no_tasks(self):
        text = "No actionable items today. Just a regular message."
        mod_text, tasks = format_message_with_tasks(text)
        assert len(tasks) == 0
        assert mod_text == text

    def test_mixed_content(self):
        text = "Your top todos:\n☐ Buy groceries\nSome info here\n☐ Fix the fence\nMore info"
        mod_text, tasks = format_message_with_tasks(text)
        assert len(tasks) == 2
        assert "[1] ☐ Buy groceries" in mod_text
        assert "[2] ☐ Fix the fence" in mod_text

    def test_empty_string(self):
        mod_text, tasks = format_message_with_tasks("")
        assert len(tasks) == 0

    def test_checkbox_without_text(self):
        """Lines with ☐ but no meaningful text should be skipped."""
        text = "☐ \n☐ Real task"
        mod_text, tasks = format_message_with_tasks(text)
        assert len(tasks) == 1
        assert "Real task" in tasks[0]["text"]

    def test_re_parses_numbered_tasks(self):
        """Verify that text already containing [number] ☐ is correctly parsed."""
        text = "[1] ☐ Open task\n[2] ☐ Another task"
        tasks = parse_tasks(text)
        assert len(tasks) == 2
        assert tasks[0]["text"] == "Open task"
        assert tasks[1]["text"] == "Another task"

    def test_parses_markdown_lists(self):
        """Verify that standard markdown lists (e.g. 1. or -) are matched and parsed correctly."""
        text = "1. ☐ Numbered list\n- ☐ Bullet list\n* ☐ Asterisk list"
        tasks = parse_tasks(text)
        assert len(tasks) == 3
        assert tasks[0]["text"] == "Numbered list"
        assert tasks[1]["text"] == "Bullet list"
        assert tasks[2]["text"] == "Asterisk list"

    def test_completed_tasks_not_matched(self):
        """Completed tasks (✅) should not be parsed."""
        text = "[1] ☐ Open task\n[2] ✅ Done task"
        tasks = parse_tasks(text)
        assert len(tasks) == 1
        assert tasks[0]["text"] == "Open task"


class TestBuildTaskKeyboard:
    def test_returns_none_for_empty(self):
        assert build_task_keyboard([]) is None

    def test_builds_keyboard_grid(self):
        tasks = [
            {"text": "Task A", "hash": "aaa", "number": 1},
            {"text": "Task B", "hash": "bbb", "number": 2},
            {"text": "Task C", "hash": "ccc", "number": 3},
            {"text": "Task D", "hash": "ddd", "number": 4},
            {"text": "Task E", "hash": "eee", "number": 5},
            {"text": "Task F", "hash": "fff", "number": 6},
        ]
        keyboard = build_task_keyboard(tasks)
        assert keyboard is not None
        # 6 tasks, max 5 per row means 2 rows total
        assert len(keyboard.inline_keyboard) == 2
        assert len(keyboard.inline_keyboard[0]) == 5
        assert len(keyboard.inline_keyboard[1]) == 1
        
        # Verify button text has numbers
        assert keyboard.inline_keyboard[0][0].text == "✅ 1"
        assert keyboard.inline_keyboard[1][0].text == "✅ 6"
        assert keyboard.inline_keyboard[0][0].callback_data == "done_aaa"

    def test_fallback_no_number(self):
        """If number isn't present, falls back to plain ✅."""
        tasks = [{"text": "Task", "hash": "abc"}]
        keyboard = build_task_keyboard(tasks)
        assert keyboard.inline_keyboard[0][0].text == "✅"


class TestRecoverTaskFromCallback:
    def test_recovers_task(self):
        text = "☐ Buy groceries\n☐ Fix the fence"
        task_hash = _hash_task("Buy groceries")
        result = recover_task_from_callback(text, task_hash)
        assert result == "Buy groceries"
        
    def test_recovers_numbered_task(self):
        text = "[1] ☐ Buy groceries\n[2] ☐ Fix the fence"
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
        text = "[1] ✅ Done task\n[2] ☐ Open task"
        task_hash = _hash_task("Open task")
        result = recover_task_from_callback(text, task_hash)
        assert result == "Open task"
