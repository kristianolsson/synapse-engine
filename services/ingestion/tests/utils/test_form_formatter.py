"""Tests for form_formatter utility module."""

from services.ingestion.utils.form_formatter import (
    format_message_with_form,
    render_form_display,
    render_form_as_html_table,
)


class TestFormatMessageWithForm:
    def test_single_yn_field(self):
        text = "Tonight's check-in:\n☐F:yn:protein Protein at every meal?\nTap to answer."
        mod_text, fields = format_message_with_form(text)
        assert len(fields) == 1
        assert fields[0] == {"type": "yn", "key": "protein", "label": "Protein at every meal?"}
        assert "☐F:" not in mod_text
        assert mod_text == "Tonight's check-in:\nTap to answer."

    def test_mixed_yn_and_text_fields(self):
        text = (
            "☐F:yn:protein Protein at every meal?\n"
            "☐F:text:sleep Sleep (bedtime / hrs)\n"
            "☐F:yn:standing_desk Standing desk?"
        )
        mod_text, fields = format_message_with_form(text)
        assert len(fields) == 3
        assert [f["key"] for f in fields] == ["protein", "sleep", "standing_desk"]
        assert [f["type"] for f in fields] == ["yn", "text", "yn"]
        assert mod_text == ""

    def test_no_form_fields(self):
        text = "Just a regular message with ☐ Buy groceries checklist item."
        mod_text, fields = format_message_with_form(text)
        assert len(fields) == 0
        assert mod_text == text

    def test_empty_string(self):
        mod_text, fields = format_message_with_form("")
        assert len(fields) == 0

    def test_invalid_type_not_matched(self):
        text = "☐F:maybe:key Some label"
        mod_text, fields = format_message_with_form(text)
        assert len(fields) == 0
        assert mod_text == text

    def test_collapses_blank_lines_left_by_stripped_fields(self):
        text = "Intro.\n\n☐F:yn:a A?\n\n☐F:yn:b B?\n\nOutro."
        mod_text, fields = format_message_with_form(text)
        assert len(fields) == 2
        assert mod_text == "Intro.\n\nOutro."


class TestRenderFormDisplay:
    def test_no_answers_returns_intro_only(self):
        result = render_form_display("Tap to answer.", [], {}, {})
        assert result == "Tap to answer."

    def test_appends_answered_fields_in_field_order(self):
        fields = [
            {"type": "yn", "key": "protein", "label": "Protein at every meal?"},
            {"type": "text", "key": "sleep", "label": "Sleep (bedtime / hrs)"},
        ]
        answers = {"protein": "Y", "sleep": "12:00 AM / 8"}
        answer_display = {"protein": "Yes", "sleep": "12:00 AM / 8"}
        result = render_form_display("Tap to answer.", fields, answers, answer_display)
        assert result == (
            "Tap to answer.\n\n"
            "✅ Protein at every meal? → Yes\n"
            "✅ Sleep (bedtime / hrs) → 12:00 AM / 8"
        )

    def test_only_answered_fields_appear(self):
        fields = [
            {"type": "yn", "key": "protein", "label": "Protein?"},
            {"type": "yn", "key": "ate_after_6", "label": "Ate after 6pm?"},
        ]
        result = render_form_display("Intro.", fields, {"protein": "Y"}, {"protein": "Yes"})
        assert "Protein?" in result
        assert "Ate after 6pm?" not in result


class TestRenderFormAsHtmlTable:
    def test_empty_fields_returns_empty_string(self):
        assert render_form_as_html_table([]) == ""

    def test_renders_yn_and_text_rows(self):
        fields = [
            {"type": "yn", "key": "protein", "label": "Protein at every meal?"},
            {"type": "text", "key": "sleep", "label": "Sleep (bedtime / hrs)"},
        ]
        result = render_form_as_html_table(fields)
        assert "<table" in result
        assert "<td>Protein at every meal?</td><td>Yes / No</td>" in result
        assert "<td>Sleep (bedtime / hrs)</td><td></td>" in result

    def test_escapes_html_special_characters_in_label(self):
        fields = [{"type": "yn", "key": "a", "label": "A & B < C?"}]
        result = render_form_as_html_table(fields)
        assert "A &amp; B &lt; C?" in result
        assert "A & B < C?" not in result
