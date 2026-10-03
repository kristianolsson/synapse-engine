"""Parse, filter, render, and diff Playwright AI aria snapshots.

Input is the YAML returned by `page.aria_snapshot(mode="ai")`. Pure functions,
no Playwright import, so everything here is unit-testable without a browser.
"""

import json
import re
from dataclasses import dataclass, field, replace
from typing import Optional

import yaml

from .errors import BrowserError

INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab",
    "switch", "slider", "spinbutton", "treeitem",
})
CONTAINER_ROLES = frozenset({
    "dialog", "alertdialog", "form", "listbox", "menu", "menubar", "tablist",
    "tree", "grid", "radiogroup", "navigation", "search",
})
# Attributes that change on focus/hover and would only add noise to diffs.
_IGNORED_ATTRS = frozenset({"active", "cursor"})

_KEY_RE = re.compile(r'^(?P<role>[A-Za-z][\w-]*)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)$')
_ATTR_RE = re.compile(r"\[([^\]=]+)(?:=([^\]]*))?\]")


@dataclass
class Node:
    role: str
    name: str = ""
    ref: Optional[str] = None
    value: Optional[str] = None
    states: tuple = ()
    children: list = field(default_factory=list)


def parse(text: str) -> list:
    try:
        data = yaml.load(text, Loader=yaml.BaseLoader) if text.strip() else None
    except yaml.YAMLError as e:
        raise BrowserError(f"could not parse page snapshot: {e}", "browser_error")
    if data is not None and not isinstance(data, list):
        raise BrowserError(f"could not parse page snapshot: expected list, got {type(data).__name__}", "browser_error")
    return [n for n in (_parse_item(i) for i in data or []) if n]


def _parse_item(item) -> Optional[Node]:
    if isinstance(item, str):
        key, rest = item, None
    elif isinstance(item, dict) and len(item) == 1:
        (key, rest), = item.items()
    else:
        return None
    key = str(key)
    if key.startswith("/") or key == "text":
        return None
    m = _KEY_RE.match(key)
    if not m:
        return None
    ref, states = None, []
    for attr, val in _ATTR_RE.findall(m["attrs"] or ""):
        if attr == "ref":
            ref = val
        elif attr not in _IGNORED_ATTRS:
            states.append(f"{attr}={val}" if val else attr)
    if m["name"] is not None:
        try:
            name = json.loads(f'"{m["name"]}"')
        except json.JSONDecodeError:
            name = m["name"]
    else:
        name = ""
    node = Node(role=m["role"], name=name, ref=ref, states=tuple(states))
    if isinstance(rest, list):
        node.children = [c for c in (_parse_item(i) for i in rest) if c]
    elif rest is not None:
        node.value = str(rest)
    return node


def _is_select_option(node: Node, parent_role: Optional[str]) -> bool:
    return node.role == "option" and node.ref is None and parent_role == "combobox"


def filter_interactive(nodes: list, parent_role: Optional[str] = None) -> list:
    """Keep interactive nodes (and named containers that hold them); splice out the rest."""
    out = []
    for n in nodes:
        kids = filter_interactive(n.children, n.role)
        interactive = (n.role in INTERACTIVE_ROLES and n.ref) or _is_select_option(n, parent_role)
        container = n.role in CONTAINER_ROLES and n.ref and kids
        if interactive or container:
            out.append(replace(n, children=kids))
        else:
            out.extend(kids)
    return out


def _select_options(node: Node) -> list:
    return [c for c in node.children if c.role == "option" and c.ref is None]


def render_line(node: Node) -> str:
    parts = [f"[{node.ref}]"] if node.ref else []
    parts.append(node.role)
    if node.name:
        parts.append(json.dumps(node.name, ensure_ascii=False))
    if node.value is not None:
        parts.append("value=" + json.dumps(node.value, ensure_ascii=False))
    parts += [f"[{s}]" for s in node.states]
    options = _select_options(node)
    if options:
        labels = " | ".join(("*" if "selected" in o.states else "") + o.name for o in options)
        parts.append(f"[options: {labels}]")
    return " ".join(parts)


def render_tree(nodes: list, depth: int = 0) -> str:
    lines = []
    for n in nodes:
        lines.append("  " * depth + render_line(n))
        kids = [c for c in n.children if c.ref]
        if kids:
            lines.append(render_tree(kids, depth + 1))
    return "\n".join(lines)


def clean_raw(raw: str) -> str:
    return re.sub(r" \[cursor=[^\]]*\]", "", raw)


def _flatten(nodes: list, container: Optional[str] = None, out: Optional[dict] = None) -> dict:
    """ref -> (node, nearest container ref), in document order."""
    out = {} if out is None else out
    for n in nodes:
        if n.ref:
            out[n.ref] = (n, container)
        is_container = n.role in CONTAINER_ROLES and n.ref
        _flatten(n.children, n.ref if is_container else container, out)
    return out


def _signature(n: Node) -> tuple:
    return (n.role, n.name, n.value, n.states, tuple(render_line(o) for o in _select_options(n)))


@dataclass
class Diff:
    added: list
    removed: list
    changed: list
    parents: dict
    by_ref: dict
    total: int

    def too_large(self) -> bool:
        changes = len(self.added) + len(self.removed) + len(self.changed)
        return self.total > 0 and changes > self.total / 2


def diff(prev: list, cur: list) -> Diff:
    p, c = _flatten(prev), _flatten(cur)
    return Diff(
        added=[n for r, (n, _) in c.items() if r not in p],
        removed=[n for r, (n, _) in p.items() if r not in c],
        changed=[(p[r][0], n) for r, (n, _) in c.items() if r in p and _signature(p[r][0]) != _signature(n)],
        parents={r: parent for r, (_, parent) in c.items()},
        by_ref={r: n for r, (n, _) in c.items()},
        total=max(len(p), len(c)),
    )


def _was(old: Node, new: Node) -> str:
    same_except_value = (old.role, old.name, old.states) == (new.role, new.name, new.states)
    if same_except_value and old.value != new.value:
        return f" (was {json.dumps(old.value or '', ensure_ascii=False)})"
    return f" (was: {render_line(replace(old, ref=None))})"


def render_diff(d: Diff) -> str:
    lines = ["~ " + render_line(new) + _was(old, new) for old, new in d.changed]
    added_refs = {n.ref for n in d.added}
    group = None
    for n in d.added:
        parent = d.parents.get(n.ref)
        if parent and parent not in added_refs:
            if parent != group:
                lines.append("in " + render_line(d.by_ref[parent]) + ":")
                group = parent
            lines.append("  + " + render_line(n))
        else:
            group = None
            lines.append("+ " + render_line(n))
    lines += ["- " + render_line(n) for n in d.removed]
    return "\n".join(lines) or "(no changes)"
