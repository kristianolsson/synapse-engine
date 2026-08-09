"""
Parsing and rendering for Actionable Forms.

Provides parsing of ☐F:type:key label lines into structured fields (the
lines are stripped from the visible message — the field list lives only
in the inline keyboard), plus a renderer for the growing "answered so
far" recap shown as fields get filled in.
"""

import html
import re

# Match lines like "☐F:yn:protein Protein at every meal?" or "☐F:text:sleep Sleep (bedtime / hrs)"
# The trailing \n? consumes the line's own newline so stripping it leaves no blank line behind.
FORM_FIELD_PATTERN = re.compile(r"^☐F:(yn|text):([A-Za-z0-9_]+)\s+(.+)$\n?", re.MULTILINE)


def format_message_with_form(text: str) -> tuple[str, list[dict]]:
    """
    Parse ☐F: field lines out of the message text.

    Returns the remaining text (framing prose, with field lines removed)
    and the list of parsed fields: [{"type": "yn"|"text", "key": "...", "label": "..."}]
    """
    fields = []

    def replacer(match):
        field_type, key, label = match.group(1), match.group(2), match.group(3).strip()
        if not label:
            return match.group(0)
        fields.append({"type": field_type, "key": key, "label": label})
        return ""

    modified_text = FORM_FIELD_PATTERN.sub(replacer, text)
    modified_text = re.sub(r"\n{3,}", "\n\n", modified_text).strip()
    return modified_text, fields


def render_form_display(intro_text: str, fields: list[dict], answers: dict, answer_display: dict) -> str:
    """Render the intro text plus a recap line for each field answered so far."""
    lines = [
        f"✅ {field['label']} → {answer_display[field['key']]}"
        for field in fields
        if field["key"] in answers
    ]
    if not lines:
        return intro_text
    return intro_text + "\n\n" + "\n".join(lines)


def render_form_as_html_table(fields: list[dict]) -> str:
    """
    Render form fields as a plain HTML table, for channels (e.g. email) that
    can't offer interactive buttons. Informational only — not fillable; the
    recipient answers by replying in text, same as before Actionable Forms.
    """
    if not fields:
        return ""
    rows = "".join(
        "<tr><td>{label}</td><td>{hint}</td></tr>".format(
            label=html.escape(field["label"]),
            hint="Yes / No" if field["type"] == "yn" else "",
        )
        for field in fields
    )
    return (
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;">'
        '<tr><th align="left">Field</th><th align="left">Answer</th></tr>'
        f"{rows}"
        "</table>"
    )
