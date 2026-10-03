"""Unit tests for snapshot parsing, filtering, rendering, and diffing."""

from services.ingestion.tools.browser.snapshot import (
    clean_raw,
    diff,
    filter_interactive,
    parse,
    render_diff,
    render_line,
    render_tree,
)

SAMPLE = '''- generic [active] [ref=e1]:
  - 'button "a: b" [ref=e2]'
  - button "\\"quoted\\" 'x'" [ref=e3]
  - textbox "Where to?" [ref=e4]: "A: B"
  - checkbox "Nonstop" [checked] [ref=e5]
  - combobox "Cabin" [ref=e6]:
    - option "Economy"
    - option "Business" [selected]
  - dialog "Pick" [ref=e7]:
    - listbox [ref=e8]:
      - option "One" [selected] [ref=e9]
  - button "Off" [disabled] [ref=e10]
  - paragraph [ref=e11]: Para text with - dash
  - link "Go" [ref=e12] [cursor=pointer]:
    - /url: /x
  - iframe [ref=e13]:
    - button "In frame" [ref=f1e2]
  - group [ref=e14]:
    - generic "More" [ref=e15]
    - text: hidden
  - textbox "Search here" [ref=e16]
'''


def _flat(nodes):
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_flat(n.children))
    return out


def test_parse_names_values_states_refs():
    root = parse(SAMPLE)[0]
    assert root.role == "generic" and root.ref == "e1"
    assert "active" not in root.states
    by_ref = {n.ref: n for n in _flat(parse(SAMPLE)) if n.ref}
    assert by_ref["e2"].name == "a: b"
    assert by_ref["e3"].name == "\"quoted\" 'x'"
    assert by_ref["e4"].value == "A: B"
    assert by_ref["e5"].states == ("checked",)
    assert by_ref["e10"].states == ("disabled",)
    assert by_ref["e12"].states == () and by_ref["e12"].children == []
    assert by_ref["f1e2"].name == "In frame"
    assert [c.name for c in by_ref["e6"].children] == ["Economy", "Business"]
    assert by_ref["e6"].children[1].states == ("selected",)
    assert by_ref["e14"].children[0].name == "More"


def test_parse_empty():
    assert parse("") == []


def test_filter_interactive_keeps_controls_and_containers():
    text = render_tree(filter_interactive(parse(SAMPLE)))
    assert text == "\n".join([
        '[e2] button "a: b"',
        '[e3] button "\\"quoted\\" \'x\'"',
        '[e4] textbox "Where to?" value="A: B"',
        '[e5] checkbox "Nonstop" [checked]',
        '[e6] combobox "Cabin" [options: Economy | *Business]',
        '[e7] dialog "Pick"',
        '  [e8] listbox',
        '    [e9] option "One" [selected]',
        '[e10] button "Off" [disabled]',
        '[e12] link "Go"',
        '[f1e2] button "In frame"',
        '[e16] textbox "Search here"',
    ])


def test_render_line_without_ref():
    n = parse('- option "Economy"\n')[0]
    assert render_line(n) == 'option "Economy"'


BEFORE = '''- generic [ref=e1]:
  - textbox "Where to?" [ref=e4]
  - button "Search" [ref=e5]
  - button "Show more" [ref=e6]
  - button "A" [ref=e20]
  - button "B" [ref=e21]
  - button "C" [ref=e22]
  - button "D" [ref=e23]
'''

AFTER = '''- generic [ref=e1]:
  - textbox "Where to?" [ref=e4]: ARN
  - button "Search" [ref=e5]
  - listbox "Suggestions" [ref=e8]:
    - option "Stockholm Arlanda (ARN)" [ref=e9]
  - button "A" [ref=e20]
  - button "B" [ref=e21]
  - button "C" [ref=e22]
  - button "D" [ref=e23]
'''


def test_diff_added_removed_changed():
    d = diff(filter_interactive(parse(BEFORE)), filter_interactive(parse(AFTER)))
    assert not d.too_large()
    assert render_diff(d) == "\n".join([
        '~ [e4] textbox "Where to?" value="ARN" (was "")',
        '+ [e8] listbox "Suggestions"',
        '+ [e9] option "Stockholm Arlanda (ARN)"',
        '- [e6] button "Show more"',
    ])


def test_diff_groups_added_under_existing_container():
    before = '- dialog "Pick" [ref=e7]:\n  - option "One" [ref=e9]\n  - button "X" [ref=e2]\n  - button "Y" [ref=e3]\n'
    after = before + '  - option "Two" [ref=e10]\n'
    d = diff(filter_interactive(parse(before)), filter_interactive(parse(after)))
    assert render_diff(d) == 'in [e7] dialog "Pick":\n  + [e10] option "Two"'


def test_diff_no_changes():
    nodes = filter_interactive(parse(BEFORE))
    assert render_diff(diff(nodes, nodes)) == "(no changes)"


def test_diff_state_change_shows_old_line():
    before = '- checkbox "Nonstop" [ref=e5]\n- button "x" [ref=e6]\n- button "y" [ref=e7]\n'
    after = '- checkbox "Nonstop" [checked] [ref=e5]\n- button "x" [ref=e6]\n- button "y" [ref=e7]\n'
    d = diff(filter_interactive(parse(before)), filter_interactive(parse(after)))
    assert render_diff(d) == '~ [e5] checkbox "Nonstop" [checked] (was: checkbox "Nonstop")'


def test_diff_select_change_detected():
    before = '- combobox "Cabin" [ref=e6]:\n  - option "Economy" [selected]\n  - option "Business"\n- button "x" [ref=e7]\n- button "y" [ref=e8]\n'
    after = '- combobox "Cabin" [ref=e6]:\n  - option "Economy"\n  - option "Business" [selected]\n- button "x" [ref=e7]\n- button "y" [ref=e8]\n'
    d = diff(filter_interactive(parse(before)), filter_interactive(parse(after)))
    assert render_diff(d).startswith('~ [e6] combobox "Cabin" [options: Economy | *Business]')


def test_too_large_when_most_nodes_change():
    d = diff(filter_interactive(parse(BEFORE)), filter_interactive(parse('- button "Z" [ref=e99]\n')))
    assert d.too_large()


def test_clean_raw_drops_cursor_noise():
    assert clean_raw('- link "Go" [ref=e12] [cursor=pointer]:') == '- link "Go" [ref=e12]:'
