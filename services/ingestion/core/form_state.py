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


def create_form(chat_id: int, user_key: str, fields: list[dict], display_text: str) -> str:
    """Register a new form and return its short id."""
    form_id = uuid.uuid4().hex[:8]
    lines = {f["key"]: f"[{f['number']}] ☐ {f['label']}" for f in fields}
    _forms[form_id] = {
        "chat_id": chat_id,
        "user_key": user_key,
        "fields": fields,
        "lines": lines,
        "answers": {},
        "display_text": display_text,
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
    """Record an answer and reflect it in the form's stored display text."""
    form = _forms.get(form_id)
    if not form or field_key in form["answers"]:
        return
    form["answers"][field_key] = raw_value
    old_line = form["lines"].get(field_key)
    if old_line:
        new_line = old_line.replace("☐", "✅", 1) + f" → {display_value}"
        form["display_text"] = form["display_text"].replace(old_line, new_line)


def register_field_prompt(prompt_message_id: int, form_id: str, field_key: str) -> None:
    """Map a sent ForceReply prompt's message_id to the field it's collecting."""
    _pending_field_prompts[prompt_message_id] = (form_id, field_key)


def pop_field_prompt(prompt_message_id: int) -> Optional[tuple[str, str]]:
    return _pending_field_prompts.pop(prompt_message_id, None)


def delete_form(form_id: str) -> None:
    _forms.pop(form_id, None)
