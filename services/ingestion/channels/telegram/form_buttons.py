"""
Inline keyboard builder for Actionable Forms.

Yes/no fields get a Yes/No button pair; short-answer fields get a single
"Answer" button that triggers a ForceReply prompt. Each button is labeled
with a short, humanized form of the field's key rather than a number, so
it's clear what you're answering without cross-referencing a list.
Answered fields drop out of the keyboard. A Submit button is always present.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _humanize(key: str) -> str:
    return key.replace("_", " ").capitalize()


def build_form_keyboard(form_id: str, fields: list[dict], answers: dict) -> InlineKeyboardMarkup:
    rows = []
    for field in fields:
        if field["key"] in answers:
            continue
        name = _humanize(field["key"])
        if field["type"] == "yn":
            rows.append([
                InlineKeyboardButton(f"✅ {name}", callback_data=f"formyn:{form_id}:{field['key']}:Y"),
                InlineKeyboardButton(f"❌ {name}", callback_data=f"formyn:{form_id}:{field['key']}:N"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(f"✏️ {name}", callback_data=f"formtext:{form_id}:{field['key']}")
            ])
    rows.append([InlineKeyboardButton("✅ Submit", callback_data=f"formsubmit:{form_id}")])
    return InlineKeyboardMarkup(rows)
