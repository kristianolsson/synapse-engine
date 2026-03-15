"""
Task formatting and parsing utilities.

Provides shared parsing, formatting, and hash recovery for
two-way task completion across channels (Telegram, Email).
"""

import hashlib
import re
from typing import Optional

# Match lines starting with optional [number] or markdown list bullet/number, then ☐ followed by task text
TASK_PATTERN_OPEN = re.compile(r"^(?:>*\s*)?(?:(?:\[\d+\]|\d+\.|-|\*)\s*)?☐\s+(.+)$", re.MULTILINE)
# Match tasks whether they are open or completed (for recovery)
TASK_PATTERN_ALL = re.compile(r"^(?:>*\s*)?(?:(?:\[\d+\]|\d+\.|-|\*)\s*)?[☐✅]\s+(.+)$", re.MULTILINE)


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
