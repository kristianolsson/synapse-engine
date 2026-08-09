"""Tests for form_state in-memory store."""

import time

from services.ingestion.core import form_state


class TestFormState:
    def test_create_and_get_form(self):
        fields = [{"type": "yn", "key": "protein", "label": "Protein?"}]
        form_id = form_state.create_form(123, "user1", fields, "Tap to answer.")
        form = form_state.get_form(form_id)
        assert form is not None
        assert form["chat_id"] == 123
        assert form["user_key"] == "user1"
        assert form["intro_text"] == "Tap to answer."
        assert form["answers"] == {}
        assert form["answer_display"] == {}
        form_state.delete_form(form_id)

    def test_apply_answer_records_raw_and_display_values(self):
        fields = [{"type": "yn", "key": "protein", "label": "Protein?"}]
        form_id = form_state.create_form(123, "user1", fields, "Tap to answer.")
        form_state.apply_answer(form_id, "protein", "Yes", "Y")
        form = form_state.get_form(form_id)
        assert form["answers"] == {"protein": "Y"}
        assert form["answer_display"] == {"protein": "Yes"}
        form_state.delete_form(form_id)

    def test_apply_answer_ignores_already_answered_field(self):
        fields = [{"type": "yn", "key": "protein", "label": "Protein?"}]
        form_id = form_state.create_form(123, "user1", fields, "Tap to answer.")
        form_state.apply_answer(form_id, "protein", "Yes", "Y")
        form_state.apply_answer(form_id, "protein", "No", "N")
        form = form_state.get_form(form_id)
        assert form["answers"]["protein"] == "Y"
        assert form["answer_display"]["protein"] == "Yes"
        form_state.delete_form(form_id)

    def test_apply_answer_missing_form_is_noop(self):
        form_state.apply_answer("nonexistent", "key", "Yes", "Y")  # should not raise

    def test_field_prompt_round_trip(self):
        form_state.register_field_prompt(999, "form1", "sleep")
        assert form_state.pop_field_prompt(999) == ("form1", "sleep")
        assert form_state.pop_field_prompt(999) is None

    def test_clear_field_prompts_for_form_only_drops_matching_form(self):
        form_state.register_field_prompt(101, "formA", "sleep")
        form_state.register_field_prompt(102, "formA", "weight")
        form_state.register_field_prompt(103, "formB", "walks")

        form_state.clear_field_prompts_for_form("formA")

        assert form_state.pop_field_prompt(101) is None
        assert form_state.pop_field_prompt(102) is None
        assert form_state.pop_field_prompt(103) == ("formB", "walks")

    def test_delete_form(self):
        form_id = form_state.create_form(123, "user1", [], "text")
        form_state.delete_form(form_id)
        assert form_state.get_form(form_id) is None


class TestPruneStaleForms:
    def test_removes_forms_older_than_max_age(self):
        old_id = form_state.create_form(123, "user1", [], "old")
        form_state.get_form(old_id)["created_at"] = 0  # far in the past

        form_state.prune_stale_forms(max_age_seconds=3600)

        assert form_state.get_form(old_id) is None

    def test_keeps_forms_within_max_age(self):
        recent_id = form_state.create_form(123, "user1", [], "recent")

        form_state.prune_stale_forms(max_age_seconds=3600)

        assert form_state.get_form(recent_id) is not None
        form_state.delete_form(recent_id)

    def test_also_drops_the_stale_form_s_pending_field_prompts(self):
        old_id = form_state.create_form(123, "user1", [], "old")
        form_state.get_form(old_id)["created_at"] = 0
        form_state.register_field_prompt(12345, old_id, "sleep")

        form_state.prune_stale_forms(max_age_seconds=3600)

        assert form_state.pop_field_prompt(12345) is None

    def test_create_form_opportunistically_prunes_stale_forms(self):
        old_id = form_state.create_form(123, "user1", [], "old")
        form_state.get_form(old_id)["created_at"] = time.time() - form_state.FORM_MAX_AGE_SECONDS - 1

        new_id = form_state.create_form(123, "user1", [], "new")

        assert form_state.get_form(old_id) is None
        assert form_state.get_form(new_id) is not None
        form_state.delete_form(new_id)
