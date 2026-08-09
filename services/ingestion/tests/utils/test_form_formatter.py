"""Tests for form_formatter utility module."""

from services.ingestion.utils.form_formatter import format_message_with_form


class TestFormatMessageWithForm:
    def test_single_yn_field(self):
        text = "Tonight's check-in:\n☐F:yn:protein Protein at every meal?\nTap to answer."
        mod_text, fields = format_message_with_form(text)
        assert len(fields) == 1
        assert fields[0] == {"type": "yn", "key": "protein", "label": "Protein at every meal?", "number": 1}
        assert "[1] ☐ Protein at every meal?" in mod_text

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
        assert [f["number"] for f in fields] == [1, 2, 3]
        assert "[2] ☐ Sleep (bedtime / hrs)" in mod_text

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
