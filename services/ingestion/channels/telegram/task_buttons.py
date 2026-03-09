"""
Task button utilities for Telegram inline keyboards.

Provides parsing, keyboard building, and hash recovery for
two-way task completion via inline keyboard buttons.
"""

import hashlib
import re
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Match lines starting with ☐ (Unicode ballot box) followed by task text
TASK_PATTERN = re.compile(r"^☐\s+(.+)$", re.MULTILINE)

# Max label length for inline keyboard buttons
MAX_BUTTON_LABEL = 40


def _hash_task(text: str) -> str:
    """Generate a short hash for callback_data (first 8 chars of SHA-256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def parse_tasks(text: str) -> list[dict]:
    """
    Extract actionable tasks from a message containing ☐ prefixed items.

    Returns a list of dicts: [{"text": "full task text", "hash": "ab12cd34"}]
    """
    tasks = []
    for match in TASK_PATTERN.finditer(text):
        task_text = match.group(1).strip()
        if task_text:
            tasks.append({"text": task_text, "hash": _hash_task(task_text)})
    return tasks


def build_task_keyboard(tasks: list[dict]) -> Optional[InlineKeyboardMarkup]:
    """
    Build an InlineKeyboardMarkup with one ✅ button per task.

    Returns None if no tasks provided.
    """
    if not tasks:
        return None

    buttons = []
    for task in tasks:
        label = task["text"]
        if len(label) > MAX_BUTTON_LABEL:
            label = label[:MAX_BUTTON_LABEL - 1] + "…"
        buttons.append([
            InlineKeyboardButton(f"✅ {label}", callback_data=f"done_{task['hash']}")
        ])

    return InlineKeyboardMarkup(buttons)


def recover_task_from_callback(message_text: str, task_hash: str) -> Optional[str]:
    """
    Re-parse tasks from the original message and match by hash
    to recover the full task text.

    Returns the full task text if found, None otherwise.
    """
    tasks = parse_tasks(message_text)
    for task in tasks:
        if task["hash"] == task_hash:
            return task["text"]
    return None
