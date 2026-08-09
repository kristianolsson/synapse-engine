"""Tests for form_state in-memory store."""

from services.ingestion.core import form_state


class TestFormState:
    def test_create_and_get_form(self):
        fields = [{"type": "yn", "key": "protein", "label": "Protein?", "number": 1}]
        form_id = form_state.create_form(123, "user1", fields, "[1] ☐ Protein?")
        form = form_state.get_form(form_id)
        assert form is not None
        assert form["chat_id"] == 123
        assert form["user_key"] == "user1"
        assert form["answers"] == {}
        form_state.delete_form(form_id)

    def test_apply_answer_updates_display_text(self):
        fields = [{"type": "yn", "key": "protein", "label": "Protein?", "number": 1}]
        form_id = form_state.create_form(123, "user1", fields, "Check-in:\n[1] ☐ Protein?\nThanks.")
        form_state.apply_answer(form_id, "protein", "Yes", "Y")
        form = form_state.get_form(form_id)
        assert form["answers"] == {"protein": "Y"}
        assert "[1] ✅ Protein? → Yes" in form["display_text"]
        assert "[1] ☐ Protein?" not in form["display_text"]
        form_state.delete_form(form_id)

    def test_apply_answer_ignores_already_answered_field(self):
        fields = [{"type": "yn", "key": "protein", "label": "Protein?", "number": 1}]
        form_id = form_state.create_form(123, "user1", fields, "[1] ☐ Protein?")
        form_state.apply_answer(form_id, "protein", "Yes", "Y")
        form_state.apply_answer(form_id, "protein", "No", "N")
        form = form_state.get_form(form_id)
        assert form["answers"]["protein"] == "Y"
        form_state.delete_form(form_id)

    def test_apply_answer_missing_form_is_noop(self):
        form_state.apply_answer("nonexistent", "key", "Yes", "Y")  # should not raise

    def test_field_prompt_round_trip(self):
        form_state.register_field_prompt(999, "form1", "sleep")
        assert form_state.pop_field_prompt(999) == ("form1", "sleep")
        assert form_state.pop_field_prompt(999) is None

    def test_delete_form(self):
        form_id = form_state.create_form(123, "user1", [], "text")
        form_state.delete_form(form_id)
        assert form_state.get_form(form_id) is None
