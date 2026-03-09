"""
Task button utilities for Telegram inline keyboards.

Provides parsing, keyboard building, and hash recovery for
two-way task completion via inline keyboard buttons.
"""

from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...utils.task_formatter import _hash_task, parse_tasks, format_message_with_tasks, recover_task_from_callback

# Max tasks per row in inline keyboard
BUTTONS_PER_ROW = 5


def build_task_keyboard(tasks: list[dict]) -> Optional[InlineKeyboardMarkup]:
    """
    Build an InlineKeyboardMarkup with a compact numbered grid.

    Returns None if no tasks provided.
    """
    if not tasks:
        return None

    buttons = []
    current_row = []
    for task in tasks:
        # Use number if available, otherwise just ✅
        num = task.get("number", "")
        label = f"✅ {num}".strip()
        
        current_row.append(
            InlineKeyboardButton(label, callback_data=f"done_{task['hash']}")
        )
        
        if len(current_row) == BUTTONS_PER_ROW:
            buttons.append(current_row)
            current_row = []
            
    if current_row:
        buttons.append(current_row)

    return InlineKeyboardMarkup(buttons)


