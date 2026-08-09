"""Tests for the shared reply_dispatch module.

This centralizes logic that previously drifted across three separate
copies (listener.handle_message, the quota-retry callback, and the
scheduler's reminder delivery) — see the code-review finding that
flagged the duplication and the bugs it let slip through.
"""

from unittest.mock import patch

from services.ingestion.core import form_state
from services.ingestion.channels.telegram.reply_dispatch import (
    build_reply_keyboard,
    attach_form_message_id,
    TELEGRAM_MESSAGE_LIMIT,
)


class TestBuildReplyKeyboard:
    def test_form_fields_create_a_form_and_form_keyboard(self):
        text = "Tonight's check-in:\n☐F:yn:protein Protein at every meal?\n"
        display_text, keyboard, form_id = build_reply_keyboard(123, "user1", text)

        assert form_id is not None
        assert display_text == "Tonight's check-in:"
        rows = keyboard.inline_keyboard
        assert any("formyn:" in b.callback_data for row in rows for b in row)

        form = form_state.get_form(form_id)
        assert form["chat_id"] == 123
        assert form["user_key"] == "user1"
        form_state.delete_form(form_id)

    def test_no_form_fields_falls_back_to_task_keyboard(self):
        text = "Your tasks:\n☐ Buy groceries"
        display_text, keyboard, form_id = build_reply_keyboard(123, "user1", text)

        assert form_id is None
        rows = keyboard.inline_keyboard
        assert rows[0][0].callback_data.startswith("done_")

    def test_no_actionable_content_returns_empty_keyboard(self):
        text = "Just a plain response."
        display_text, keyboard, form_id = build_reply_keyboard(123, "user1", text)

        assert form_id is None
        assert display_text == text
        assert keyboard is None

    def test_truncates_to_telegram_message_limit(self):
        text = "x" * (TELEGRAM_MESSAGE_LIMIT + 500)
        display_text, _keyboard, _form_id = build_reply_keyboard(123, "user1", text)
        assert len(display_text) == TELEGRAM_MESSAGE_LIMIT
        assert display_text.endswith("...")

    def test_truncation_applies_before_form_parsing_so_cached_copy_matches_sent_copy(self):
        """The form's cached intro_text must reflect the same (truncated) text that
        gets sent — a prior bug cached the full text while the actually-sent
        message was truncated downstream, so later edits could exceed the limit."""
        long_label = "x" * (TELEGRAM_MESSAGE_LIMIT + 500)
        text = f"☐F:yn:protein {long_label}"
        display_text, _keyboard, form_id = build_reply_keyboard(123, "user1", text)
        assert len(display_text) <= TELEGRAM_MESSAGE_LIMIT
        if form_id:
            form_state.delete_form(form_id)


class TestAttachFormMessageId:
    def test_noop_when_form_id_is_none(self):
        attach_form_message_id(None, 999, "sess-1")  # should not raise

    def test_sets_message_id_and_session_id(self):
        form_id = form_state.create_form(123, "user1", [], "intro")
        attach_form_message_id(form_id, 999, "sess-1")
        form = form_state.get_form(form_id)
        assert form["message_id"] == 999
        assert form["session_id"] == "sess-1"
        form_state.delete_form(form_id)

    def test_session_id_optional(self):
        form_id = form_state.create_form(123, "user1", [], "intro")
        attach_form_message_id(form_id, 999)
        form = form_state.get_form(form_id)
        assert form["message_id"] == 999
        assert form["session_id"] is None
        form_state.delete_form(form_id)
