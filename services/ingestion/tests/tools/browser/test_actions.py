"""Integration tests for browser command handlers against real headless Firefox."""

import json
import re

import pytest
import pytest_asyncio

from services.ingestion.tests.tools.browser.helpers import ref_of
from services.ingestion.tools.browser.actions import Actions
from services.ingestion.tools.browser.errors import BrowserError

pytestmark = pytest.mark.browser


@pytest_asyncio.fixture
async def act(session):
    return Actions(session)


async def test_open_returns_full_snapshot(act, site):
    resp = await act.execute({"cmd": "open", "url": site + "/form.html"})
    h = resp["header"]
    assert h["title"] == "Form" and h["navigated"] is True and h["settled"] is True
    assert 'textbox "Name"' in resp["body"]
    assert '[options: Economy | Business]' in resp["body"] or '[options: *Economy | Business]' in resp["body"]


async def test_open_rejects_bad_scheme(act):
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "open", "url": "file:///etc/passwd"})
    assert e.value.code == "policy_blocked"


async def test_fill_select_check_click_flow(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/form.html"}))["body"]
    resp = await act.execute({"cmd": "fill", "ref": ref_of(body, "textbox", "Name"), "value": "Ada"})
    assert 'value="Ada"' in resp["body"] and resp["header"]["navigated"] is False
    resp = await act.execute({"cmd": "select", "ref": ref_of(body, "combobox", "Cabin"), "value": "Business"})
    assert "*Business" in resp["body"]
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "checkbox", "Nonstop")})
    assert "[checked]" in resp["body"]
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "button", "Submit"), "text": True})
    assert 'Submitted {"name":"Ada","cabin":"Business","nonstop":true}' in resp["body"]


async def test_type_pick_autocomplete(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/autocomplete.html"}))["body"]
    combo = ref_of(body, "combobox", "Where from?")
    resp = await act.execute({"cmd": "type", "ref": combo, "value": "Alp", "pick": 2})
    assert 'value="Alphaville (AAV)"' in resp["body"]


async def test_type_without_pick_returns_options_diff(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/autocomplete.html"}))["body"]
    resp = await act.execute({"cmd": "type", "ref": ref_of(body, "combobox", "Where from?"), "value": "Bra"})
    # Most of this tiny page changed, so the response falls back to a full snapshot.
    assert 'option "Bravo Bay (BBB)"' in resp["body"]
    assert "--- page text ---" not in resp["body"]


async def test_type_pick_no_options(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/autocomplete.html"}))["body"]
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "type", "ref": ref_of(body, "combobox", "Where from?"), "value": "zzz", "pick": 1})
    assert e.value.code == "no_options"


async def test_click_xhr_returns_diff_after_settle(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/xhr.html"}))["body"]
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "button", "Load results")})
    assert resp["header"]["settled"] is True
    assert '+ [' in resp["body"] and 'button "Next page"' in resp["body"]


async def test_busy_page_reports_unsettled(act, site):
    resp = await act.execute({"cmd": "open", "url": site + "/busy.html"})
    assert resp["header"]["settled"] is False


async def test_navigation_gives_full_snapshot_and_stale_refs(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/nav.html"}))["body"]
    other = ref_of(body, "button", "Other button")
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "link", "Go to page two")})
    assert resp["header"]["navigated"] is True and 'button "Page two button"' in resp["body"]
    assert not resp["body"].startswith(("+", "~", "-"))
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "click", "ref": other})
    assert e.value.code == "stale_ref"


async def test_pushstate_keeps_refs(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/nav.html"}))["body"]
    other = ref_of(body, "button", "Other button")
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "button", "Virtual route")})
    assert resp["header"]["navigated"] is False and resp["header"]["url"].endswith("/virtual")
    resp = await act.execute({"cmd": "click", "ref": other, "text": True})
    assert "Other clicked" in resp["body"]


async def test_invalid_and_unknown_refs(act, site):
    await act.execute({"cmd": "open", "url": site + "/nav.html"})
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "click", "ref": "button"})
    assert e.value.code == "usage_error"
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "click", "ref": "e9999"})
    assert e.value.code == "stale_ref"


async def test_wait_for_text(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/xhr.html"}))["body"]
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "button", "Load results"), "wait_for": "Loaded results", "snapshot": "none"})
    assert resp["body"] == ""
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "wait", "wait_for": "Never appears", "timeout": 1})
    assert e.value.code == "timeout"


async def test_popup_switches_tab_and_tabs_command(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/nav.html"}))["body"]
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "link", "Open in new tab")})
    assert resp["header"]["tabs"] == 2 and resp["header"]["tab"] == 2 and resp["header"]["new_tab"] is True
    listing = (await act.execute({"cmd": "tabs"}))["body"]
    assert "1. Nav" in listing and "* 2. Page two" in listing
    resp = await act.execute({"cmd": "tabs", "switch": 1})
    assert resp["header"]["tab"] == 1 and 'button "Virtual route"' in resp["body"]


async def test_dialog_reported_in_header(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/dialog.html"}))["body"]
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "button", "Delete"), "accept_dialog": True, "text": True})
    assert resp["header"]["dialogs"] == [{"type": "confirm", "message": "Really delete?", "action": "accepted"}]
    assert "confirmed" in resp["body"]


async def test_download_reported(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/download.html"}))["body"]
    resp = await act.execute({"cmd": "click", "ref": ref_of(body, "link", "Get report")})
    assert resp["header"]["downloads"][0].endswith("report.csv")


async def test_text_selector_and_truncation(act, site):
    await act.execute({"cmd": "open", "url": site + "/page2.html"})
    assert (await act.execute({"cmd": "text"}))["body"].startswith("Page two")
    assert (await act.execute({"cmd": "text", "selector": "p"}))["body"] == "Second page body text."
    resp = await act.execute({"cmd": "text", "max": 5})
    assert resp["body"].startswith("Page ") and "[truncated" in resp["body"]
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "text", "selector": "#missing"})
    assert e.value.code == "not_found"


async def test_snapshot_all_and_press(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/form.html"}))["body"]
    full = (await act.execute({"cmd": "snapshot", "all": True}))["body"]
    assert "cursor=" not in full and "[ref=" in full
    resp = await act.execute({"cmd": "press", "key": "Enter", "ref": ref_of(body, "textbox", "Name"), "text": True})
    assert "Enter pressed" in resp["body"]


async def test_screenshot_and_eval(act, site):
    await act.execute({"cmd": "open", "url": site + "/page2.html"})
    resp = await act.execute({"cmd": "screenshot"})
    assert resp["header"]["screenshot"].endswith(".png")
    assert (await act.execute({"cmd": "eval", "js": "document.title"}))["body"] == json.dumps("Page two")
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "eval", "js": "(() => { throw new Error('boom') })()"})
    assert e.value.code == "eval_error"
    assert "boom" not in e.value.message  # page-controlled text stays out of the message
    assert "boom" in e.value.details["untrusted"]


async def test_back(act, site):
    body = (await act.execute({"cmd": "open", "url": site + "/nav.html"}))["body"]
    await act.execute({"cmd": "click", "ref": ref_of(body, "link", "Go to page two")})
    resp = await act.execute({"cmd": "back"})
    assert resp["header"]["title"] == "Nav" and resp["header"]["navigated"] is True


async def test_run_batch_and_failure(act, site):
    resp = await act.execute({"cmd": "run", "steps": [
        {"cmd": "open", "url": site + "/form.html"},
        {"cmd": "snapshot"},
    ]})
    assert [s["cmd"] for s in resp["header"]["steps"]] == ["open", "snapshot"]
    assert 'textbox "Name"' in resp["body"]
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "run", "steps": [{"cmd": "snapshot"}, {"cmd": "click", "ref": "e9999"}]})
    assert e.value.code == "stale_ref" and e.value.message.startswith("step 2 (click) failed")
    assert e.value.details["completed"][0]["cmd"] == "snapshot"
    assert "url" not in e.value.details["completed"][0]  # page-derived, kept out of stderr


async def test_run_failure_keeps_inner_untrusted_details(act, site):
    await act.execute({"cmd": "open", "url": site + "/page2.html"})
    with pytest.raises(BrowserError) as e:
        await act.execute({"cmd": "run", "steps": [{"cmd": "eval", "js": "(() => { throw new Error('boom') })()"}]})
    assert "boom" not in e.value.message
    assert "boom" in e.value.details["untrusted"]


async def test_unknown_command_and_bad_mode(act):
    for req in ({"cmd": "nope"}, {"cmd": "snapshot", "snapshot": "weird"}, {"cmd": "run", "steps": []}):
        with pytest.raises(BrowserError) as e:
            await act.execute(req)
        assert e.value.code == "usage_error"


async def test_display_refs_stable_within_doc_and_unique_across_docs(act, site):
    first = (await act.execute({"cmd": "open", "url": site + "/nav.html"}))["body"]
    again = (await act.execute({"cmd": "snapshot"}))["body"]
    assert again == first
    nav_refs = set(re.findall(r"\[(e\d+)\]", first))
    assert nav_refs
    page2 = (await act.execute({"cmd": "open", "url": site + "/page2.html"}))["body"]
    page2_refs = set(re.findall(r"\[(e\d+)\]", page2))
    assert page2_refs and not (nav_refs & page2_refs)
