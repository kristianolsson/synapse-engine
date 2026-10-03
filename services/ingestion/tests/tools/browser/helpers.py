"""Helpers for browser tests."""

import json
import re


def ref_of(body: str, role: str, name: str) -> str:
    """Find the ref for `role "name"` in rendered snapshot/diff text."""
    m = re.search(r"\[((?:f\d+)?e\d+)\] " + re.escape(f"{role} {json.dumps(name)}"), body)
    assert m, f"{role} {name!r} not found in:\n{body}"
    return m.group(1)
