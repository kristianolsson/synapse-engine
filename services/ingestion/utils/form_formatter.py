"""
Parsing for Actionable Forms.

Provides parsing of ☐F:type:key label lines into structured fields, and
rewrites them into numbered checklist-style display lines, mirroring
task_formatter.py's handling of plain ☐ checklist items.
"""

import re

# Match lines like "☐F:yn:protein Protein at every meal?" or "☐F:text:sleep Sleep (bedtime / hrs)"
FORM_FIELD_PATTERN = re.compile(r"^☐F:(yn|text):([A-Za-z0-9_]+)\s+(.+)$", re.MULTILINE)


def format_message_with_form(text: str) -> tuple[str, list[dict]]:
    """
    Parse ☐F: field lines and rewrite them as numbered checklist-style lines.

    Returns the modified text and the list of parsed fields:
    [{"type": "yn"|"text", "key": "...", "label": "...", "number": N}]
    """
    fields = []
    counter = 1

    def replacer(match):
        nonlocal counter
        field_type, key, label = match.group(1), match.group(2), match.group(3).strip()
        if not label:
            return match.group(0)
        fields.append({"type": field_type, "key": key, "label": label, "number": counter})
        result = f"[{counter}] ☐ {label}"
        counter += 1
        return result

    modified_text = FORM_FIELD_PATTERN.sub(replacer, text)
    return modified_text, fields
