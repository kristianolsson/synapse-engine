"""Integration tests for Session and settle() against real headless Firefox."""

import time

import pytest

from services.ingestion.tools.browser.errors import BrowserError
from services.ingestion.tools.browser.settle import doc_id, settle, wait_for_condition
from services.ingestion.tools.browser.session import Session
from services.ingestion.tools.browser.snapshot import render_tree

pytestmark = pytest.mark.browser


async def _goto(session, url):
    page = await session.ensure_page()
    await page.goto(url)
    return page


async def test_init_script_sets_doc_id(session, site):
    page = await _goto(session, site + "/nav.html")
    first = await doc_id(page)
    assert first
    await page.goto(site + "/page2.html")
    assert await doc_id(page) not in (None, first)


async def test_snapshot_stores_state(session, site):
    page = await _goto(session, site + "/nav.html")
    raw, prev, cur = await session.snapshot(page)
    assert prev is None and "[ref=" in raw
    assert 'button "Virtual route"' in render_tree(cur.nodes)
    _, prev2, _ = await session.snapshot(page)
    assert prev2 is cur
    assert session.last[page] is not cur


async def test_settle_waits_for_xhr(session, site):
    page = await _goto(session, site + "/xhr.html")
    before = await doc_id(page)
    await page.get_by_role("button", name="Load results").click()
    t0 = time.monotonic()
    settled, navigated = await settle(page, session.trackers[page], before)
    assert settled and not navigated
    assert time.monotonic() - t0 >= 1.0
    assert await page.get_by_role("button", name="Next page").count() == 1


async def test_settle_caps_on_busy_page(session, site):
    page = await _goto(session, site + "/busy.html")
    t0 = time.monotonic()
    settled, _ = await settle(page, session.trackers[page], await doc_id(page), cap=2.0)
    assert not settled
    assert 1.9 <= time.monotonic() - t0 < 4


async def test_settle_detects_navigation(session, site):
    page = await _goto(session, site + "/nav.html")
    before = await doc_id(page)
    await page.get_by_role("link", name="Go to page two").click()
    settled, navigated = await settle(page, session.trackers[page], before)
    assert settled and navigated
    assert await page.title() == "Page two"


async def test_pushstate_is_not_navigation(session, site):
    page = await _goto(session, site + "/nav.html")
    before = await doc_id(page)
    await page.get_by_role("button", name="Virtual route").click()
    settled, navigated = await settle(page, session.trackers[page], before)
    assert settled and not navigated
    assert page.url.endswith("/virtual")


async def test_popup_becomes_current_tab(session, site):
    page = await _goto(session, site + "/nav.html")
    session.begin_command(accept_dialog=False)
    await page.get_by_role("link", name="Open in new tab").click()
    for _ in range(50):
        if len(session.pages) == 2:
            break
        await page.wait_for_timeout(100)
    assert len(session.pages) == 2 and session.page is not page
    assert session.events["new_tab"] is True


async def test_dialog_dismissed_by_default_and_accepted_on_request(session, site):
    page = await _goto(session, site + "/dialog.html")
    session.begin_command(accept_dialog=False)
    await page.get_by_role("button", name="Delete").click()
    await page.wait_for_function("document.getElementById('out').textContent !== ''")
    assert await page.text_content("#out") == "cancelled"
    assert session.events["dialogs"] == [{"type": "confirm", "message": "Really delete?", "action": "dismissed"}]
    session.begin_command(accept_dialog=True)
    await page.get_by_role("button", name="Delete").click()
    await page.wait_for_function("document.getElementById('out').textContent === 'confirmed'")
    assert session.events["dialogs"][0]["action"] == "accepted"


async def test_download_saved_to_artifacts(session, site):
    page = await _goto(session, site + "/download.html")
    session.begin_command(accept_dialog=False)
    before = await doc_id(page)
    await page.get_by_role("link", name="Get report").click()
    await settle(page, session.trackers[page], before)
    await session.drain_downloads(5)
    [path] = session.events["downloads"]
    assert path.endswith("report.csv")
    assert (session.artifacts / "report.csv").read_text() == "a,b\n1,2\n"


async def test_header_fields(session, site):
    page = await _goto(session, site + "/page2.html")
    session.begin_command(accept_dialog=False)
    h = await session.header(page, True, False)
    assert h["url"].endswith("/page2.html") and h["title"] == "Page two"
    assert h["settled"] is True and h["navigated"] is False and h["tab"] == 1 and h["tabs"] == 1
    assert "dialogs" not in h  # empty events are omitted


async def test_private_network_blocked_without_allow_private(tmp_path, site):
    s = Session(profile=tmp_path / "p", artifacts=tmp_path / "a", allow_private=False)
    await s.start()
    try:
        page = await s.ensure_page()
        s.begin_command(accept_dialog=False)
        with pytest.raises(Exception):
            await page.goto(site + "/page2.html")
        assert "127.0.0.1" in s.events["blocked"]
    finally:
        await s.close()


async def test_closing_earlier_tab_keeps_current_page(session, site):
    page = await _goto(session, site + "/nav.html")
    await page.get_by_role("link", name="Open in new tab").click()
    for _ in range(50):
        if len(session.pages) == 2:
            break
        await page.wait_for_timeout(100)
    third = await session.context.new_page()
    for _ in range(50):
        if len(session.pages) == 3:
            break
        await page.wait_for_timeout(100)
    session.current = 1
    chosen = session.page
    await session.pages[0].close()
    for _ in range(50):
        if len(session.pages) == 2:
            break
        await chosen.wait_for_timeout(100)
    assert len(session.pages) == 2
    assert session.page is chosen
    assert third in session.pages


async def test_websocket_to_private_host_blocked(tmp_path, site):
    # Port 9 would be refused by Firefox's banned-port list before reaching the proxy, so aim
    # at the live fixture server's port.
    port = site.rsplit(":", 1)[1]
    s = Session(profile=tmp_path / "p", artifacts=tmp_path / "a", allow_private=False)
    await s.start()
    try:
        page = await s.ensure_page()
        s.begin_command(accept_dialog=False)
        await page.evaluate(f"""() => new Promise(done => {{
            const ws = new WebSocket('ws://127.0.0.1:{port}/');
            ws.onerror = ws.onclose = () => done();
        }})""")
        assert "127.0.0.1" in s.events["blocked"]
    finally:
        await s.close()


async def test_wait_for_condition_uses_first_visible_text(session):
    page = await session.ensure_page()
    await page.set_content("<p hidden>Ready</p><p>Ready</p>")
    await wait_for_condition(page, "Ready", None, 2)


async def test_wait_for_condition_timeout_names_selector(session):
    page = await session.ensure_page()
    await page.set_content("<p>Ready</p>")
    with pytest.raises(BrowserError) as ei:
        await wait_for_condition(page, "Ready", "#missing", 1)
    assert ei.value.code == "timeout" and "#missing" in ei.value.message


async def test_start_failure_after_launch_closes_browser(tmp_path):
    from unittest.mock import patch

    from services.ingestion.tools.browser import runtime

    profile = tmp_path / "profile"
    s = Session(profile=profile, artifacts=tmp_path / "artifacts", allow_private=True)
    with patch.object(Session, "_attach", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            await s.start()
    assert runtime.find_browser(profile) is None


class _NavigatingPage:
    """Fake page whose document changes while aria_snapshot runs."""

    def __init__(self):
        self.doc = "old-doc"

    async def aria_snapshot(self, mode):
        self.doc = "new-doc"  # a navigation lands mid-snapshot
        return '- button "Old" [ref=e1]'

    async def evaluate(self, js):
        return self.doc


async def test_snapshot_reads_doc_id_before_aria_snapshot(tmp_path):
    s = Session(profile=tmp_path / "p", artifacts=tmp_path / "a", allow_private=True)
    page = _NavigatingPage()
    _raw, _prev, state = await s.snapshot(page)
    # Refs from the old document must be filed under the old doc id, so they go stale.
    assert state.doc == "old-doc"


class _LocalhostOnlyGuard:
    """Blocks only the name 'localhost', so 127.0.0.1 stays reachable for the fixture server."""

    async def addresses(self, host: str):
        return None if host == "localhost" else [host]


async def _guarded_session(tmp_path):
    s = Session(profile=tmp_path / "p", artifacts=tmp_path / "a", allow_private=False)
    s._guard = _LocalhostOnlyGuard()
    await s.start()
    return s


async def test_redirect_to_blocked_host_is_blocked(tmp_path, site):
    from .conftest import REQUEST_LOG

    port = site.rsplit(":", 1)[1]
    s = await _guarded_session(tmp_path)
    try:
        page = await s.ensure_page()
        s.begin_command(accept_dialog=False)
        REQUEST_LOG.clear()
        with pytest.raises(Exception):
            await page.goto(f"{site}/redirect?to=http://localhost:{port}/page2.html")
        assert s.events["blocked"] == ["localhost"]
        assert not page.url.startswith("http://localhost")
        assert ("127.0.0.1:" + port, "/redirect") in REQUEST_LOG
        assert (f"localhost:{port}", "/page2.html") not in REQUEST_LOG
    finally:
        await s.close()


async def test_two_hop_redirect_to_blocked_host_is_blocked(tmp_path, site):
    from urllib.parse import quote

    from .conftest import REQUEST_LOG

    port = site.rsplit(":", 1)[1]
    inner = f"{site}/redirect?to=http://localhost:{port}/page2.html"
    s = await _guarded_session(tmp_path)
    try:
        page = await s.ensure_page()
        s.begin_command(accept_dialog=False)
        REQUEST_LOG.clear()
        with pytest.raises(Exception):
            await page.goto(f"{site}/redirect?to={quote(inner, safe='')}")
        assert s.events["blocked"] == ["localhost"]
        assert len([r for r in REQUEST_LOG if r[1] == "/redirect"]) == 2
        assert (f"localhost:{port}", "/page2.html") not in REQUEST_LOG
    finally:
        await s.close()


async def test_redirect_to_allowed_host_still_works(tmp_path, site):
    s = await _guarded_session(tmp_path)
    try:
        page = await s.ensure_page()
        s.begin_command(accept_dialog=False)
        await page.goto(f"{site}/redirect?to={site}/page2.html")
        assert page.url == site + "/page2.html"
        assert await page.title() == "Page two"
        assert s.events["blocked"] == []
    finally:
        await s.close()


async def test_websocket_to_blocked_host_via_proxy(tmp_path, site):
    port = site.rsplit(":", 1)[1]
    s = await _guarded_session(tmp_path)
    try:
        page = await s.ensure_page()
        await page.goto(site + "/page2.html")
        s.begin_command(accept_dialog=False)
        await page.evaluate(f"""() => new Promise(done => {{
            const ws = new WebSocket('ws://localhost:{port}/');
            ws.onerror = ws.onclose = () => done();
        }})""")
        assert s.events["blocked"] == ["localhost"]
    finally:
        await s.close()


async def test_worker_fetch_to_blocked_host_is_blocked(tmp_path, site):
    from .conftest import REQUEST_LOG

    port = site.rsplit(":", 1)[1]
    s = await _guarded_session(tmp_path)
    try:
        page = await s.ensure_page()
        await page.goto(site + "/page2.html")
        s.begin_command(accept_dialog=False)
        REQUEST_LOG.clear()
        result = await page.evaluate(f"""() => new Promise(done => {{
            const src = "fetch('http://localhost:{port}/page2.html', {{mode: 'no-cors'}})"
                + ".then(() => postMessage('loaded'), () => postMessage('failed'))";
            const w = new Worker(URL.createObjectURL(new Blob([src])));
            w.onmessage = e => done(e.data);
        }})""")
        assert result == "failed"
        assert s.events["blocked"] == ["localhost"]
        assert (f"localhost:{port}", "/page2.html") not in REQUEST_LOG
    finally:
        await s.close()


async def test_close_stops_proxy(tmp_path):
    import asyncio

    s = Session(profile=tmp_path / "p", artifacts=tmp_path / "a", allow_private=False)
    await s.start()
    port = s._proxy.port
    await s.close()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_allow_private_runs_without_proxy(session):
    assert session._proxy is None
