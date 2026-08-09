"""
In-memory state for interactive Telegram Actionable Forms.

Forms are ephemeral: created when a message containing ☐F: fields is sent,
discarded once submitted. No persistence — a bot restart drops any
in-flight form, same as the existing quota-retry and re-auth pending state
in listener.py.
"""

import time
import uuid
from typing import Optional

_forms: dict[str, dict] = {}
_pending_field_prompts: dict[int, tuple[str, str]] = {}


def create_form(chat_id: int, user_key: str, fields: list[dict], intro_text: str) -> str:
    """Register a new form and return its short id."""
    form_id = uuid.uuid4().hex[:8]
    _forms[form_id] = {
        "chat_id": chat_id,
        "user_key": user_key,
        "fields": fields,
        "answers": {},
        "answer_display": {},
        "intro_text": intro_text,
        "message_id": None,
        "session_id": None,
        "created_at": time.time(),
    }
    return form_id


def get_form(form_id: str) -> Optional[dict]:
    return _forms.get(form_id)


def set_message_id(form_id: str, message_id: int) -> None:
    form = _forms.get(form_id)
    if form:
        form["message_id"] = message_id


def set_session_id(form_id: str, session_id: str) -> None:
    form = _forms.get(form_id)
    if form:
        form["session_id"] = session_id


def apply_answer(form_id: str, field_key: str, display_value: str, raw_value: str) -> None:
    """Record an answer for a field. No-ops if the form is gone or already answered."""
    form = _forms.get(form_id)
    if not form or field_key in form["answers"]:
        return
    form["answers"][field_key] = raw_value
    form["answer_display"][field_key] = display_value


def register_field_prompt(prompt_message_id: int, form_id: str, field_key: str) -> None:
    """Map a sent ForceReply prompt's message_id to the field it's collecting."""
    _pending_field_prompts[prompt_message_id] = (form_id, field_key)


def pop_field_prompt(prompt_message_id: int) -> Optional[tuple[str, str]]:
    return _pending_field_prompts.pop(prompt_message_id, None)


def clear_field_prompts_for_form(form_id: str) -> None:
    """Drop any still-open ForceReply prompts for this form (e.g. on submit)."""
    stale_ids = [mid for mid, (fid, _key) in _pending_field_prompts.items() if fid == form_id]
    for mid in stale_ids:
        del _pending_field_prompts[mid]


def delete_form(form_id: str) -> None:
    _forms.pop(form_id, None)
