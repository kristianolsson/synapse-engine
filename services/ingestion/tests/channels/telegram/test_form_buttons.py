"""Tests for form_buttons utility module."""

from services.ingestion.channels.telegram.form_buttons import build_form_keyboard


class TestBuildFormKeyboard:
    def test_yn_field_gets_yes_no_row_with_humanized_label(self):
        fields = [{"type": "yn", "key": "standing_desk", "label": "Standing desk?"}]
        keyboard = build_form_keyboard("f1", fields, {})
        rows = keyboard.inline_keyboard
        assert len(rows) == 2  # yn row + submit row
        assert [b.text for b in rows[0]] == ["✅ Standing desk", "❌ Standing desk"]
        assert rows[0][0].callback_data == "formyn:f1:standing_desk:Y"
        assert rows[0][1].callback_data == "formyn:f1:standing_desk:N"

    def test_text_field_gets_answer_button_with_humanized_label(self):
        fields = [{"type": "text", "key": "sleep", "label": "Sleep"}]
        keyboard = build_form_keyboard("f1", fields, {})
        rows = keyboard.inline_keyboard
        assert len(rows) == 2  # text row + submit row
        assert rows[0][0].text == "✏️ Sleep"
        assert rows[0][0].callback_data == "formtext:f1:sleep"

    def test_submit_button_always_present(self):
        keyboard = build_form_keyboard("f1", [], {})
        rows = keyboard.inline_keyboard
        assert len(rows) == 1
        assert rows[0][0].callback_data == "formsubmit:f1"

    def test_answered_fields_dropped_from_keyboard(self):
        fields = [
            {"type": "yn", "key": "protein", "label": "Protein?"},
            {"type": "text", "key": "sleep", "label": "Sleep"},
        ]
        keyboard = build_form_keyboard("f1", fields, {"protein": "Y"})
        rows = keyboard.inline_keyboard
        # protein row dropped, sleep row + submit row remain
        assert len(rows) == 2
        assert rows[0][0].callback_data == "formtext:f1:sleep"

    def test_field_order_preserved(self):
        """Booleans-first ordering comes from field list order (table column order)."""
        fields = [
            {"type": "yn", "key": "protein", "label": "Protein?"},
            {"type": "yn", "key": "ate_after_6", "label": "Ate after 6pm?"},
            {"type": "text", "key": "sleep", "label": "Sleep"},
        ]
        keyboard = build_form_keyboard("f1", fields, {})
        rows = keyboard.inline_keyboard
        assert "formyn:f1:protein" in rows[0][0].callback_data
        assert "formyn:f1:ate_after_6" in rows[1][0].callback_data
        assert "formtext:f1:sleep" in rows[2][0].callback_data
