"""
Inline keyboard builder for Actionable Forms.

Yes/no fields get a Yes/No button pair; short-answer fields get a single
"Answer" button that triggers a ForceReply prompt. Answered fields drop
out of the keyboard. A Submit button is always present.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_form_keyboard(form_id: str, fields: list[dict], answers: dict) -> InlineKeyboardMarkup:
    rows = []
    for field in fields:
        if field["key"] in answers:
            continue
        num = field["number"]
        if field["type"] == "yn":
            rows.append([
                InlineKeyboardButton(f"{num}. Yes", callback_data=f"formyn:{form_id}:{field['key']}:Y"),
                InlineKeyboardButton(f"{num}. No", callback_data=f"formyn:{form_id}:{field['key']}:N"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(f"{num}. ✏️ Answer", callback_data=f"formtext:{form_id}:{field['key']}")
            ])
    rows.append([InlineKeyboardButton("✅ Submit", callback_data=f"formsubmit:{form_id}")])
    return InlineKeyboardMarkup(rows)
