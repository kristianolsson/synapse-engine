"""
Task button utilities for Telegram inline keyboards.

Provides parsing, keyboard building, and hash recovery for
two-way task completion via inline keyboard buttons.
"""

import hashlib
import re
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Match lines starting with optional [number] then ☐ followed by task text
TASK_PATTERN_OPEN = re.compile(r"^(?:\[\d+\]\s*)?☐\s+(.+)$", re.MULTILINE)
# Match tasks whether they are open or completed (for recovery)
TASK_PATTERN_ALL = re.compile(r"^(?:\[\d+\]\s*)?[☐✅]\s+(.+)$", re.MULTILINE)

# Max tasks per row in inline keyboard
BUTTONS_PER_ROW = 5


def _hash_task(text: str) -> str:
    """Generate a short hash for callback_data (first 8 chars of SHA-256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def parse_tasks(text: str) -> list[dict]:
    """
    Extract actionable tasks from a message containing ☐ prefixed items.

    Returns a list of dicts: [{"text": "full task text", "hash": "ab12cd34"}]
    """
    tasks = []
    for match in TASK_PATTERN_OPEN.finditer(text):
        task_text = match.group(1).strip()
        if task_text:
            tasks.append({"text": task_text, "hash": _hash_task(task_text)})
    return tasks


def format_message_with_tasks(text: str) -> tuple[str, list[dict]]:
    """
    Parse tasks and rewrite the message text to inject [1], [2] numbers before tasks.
    Returns the modified text and the list of parsed tasks with their numbers.
    """
    tasks = []
    counter = 1
    
    def replacer(match):
        nonlocal counter
        task_text = match.group(1).strip()
        if not task_text:
            return match.group(0)
            
        task_hash = _hash_task(task_text)
        tasks.append({
            "text": task_text,
            "hash": task_hash,
            "number": counter
        })
        result = f"[{counter}] ☐ {task_text}"
        counter += 1
        return result

    modified_text = TASK_PATTERN_OPEN.sub(replacer, text)
    return modified_text, tasks


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


def recover_task_from_callback(message_text: str, task_hash: str) -> Optional[str]:
    """
    Re-parse tasks from the original message and match by hash
    to recover the full task text.
    
    Supports recovering tasks that have already been marked complete (✅).

    Returns the full task text if found, None otherwise.
    """
    for match in TASK_PATTERN_ALL.finditer(message_text):
        task_text = match.group(1).strip()
        if task_text and _hash_task(task_text) == task_hash:
            return task_text
    return None
