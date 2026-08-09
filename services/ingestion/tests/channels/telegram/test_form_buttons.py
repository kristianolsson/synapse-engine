"""Tests for form_buttons utility module."""

from services.ingestion.channels.telegram.form_buttons import build_form_keyboard


class TestBuildFormKeyboard:
    def test_yn_field_gets_yes_no_row(self):
        fields = [{"type": "yn", "key": "protein", "label": "Protein?", "number": 1}]
        keyboard = build_form_keyboard("f1", fields, {})
        rows = keyboard.inline_keyboard
        assert len(rows) == 2  # yn row + submit row
        assert [b.text for b in rows[0]] == ["1. Yes", "1. No"]
        assert rows[0][0].callback_data == "formyn:f1:protein:Y"
        assert rows[0][1].callback_data == "formyn:f1:protein:N"

    def test_text_field_gets_answer_button(self):
        fields = [{"type": "text", "key": "sleep", "label": "Sleep", "number": 2}]
        keyboard = build_form_keyboard("f1", fields, {})
        rows = keyboard.inline_keyboard
        assert len(rows) == 2  # text row + submit row
        assert rows[0][0].callback_data == "formtext:f1:sleep"

    def test_submit_button_always_present(self):
        keyboard = build_form_keyboard("f1", [], {})
        rows = keyboard.inline_keyboard
        assert len(rows) == 1
        assert rows[0][0].callback_data == "formsubmit:f1"

    def test_answered_fields_dropped_from_keyboard(self):
        fields = [
            {"type": "yn", "key": "protein", "label": "Protein?", "number": 1},
            {"type": "text", "key": "sleep", "label": "Sleep", "number": 2},
        ]
        keyboard = build_form_keyboard("f1", fields, {"protein": "Y"})
        rows = keyboard.inline_keyboard
        # protein row dropped, sleep row + submit row remain
        assert len(rows) == 2
        assert rows[0][0].callback_data == "formtext:f1:sleep"
