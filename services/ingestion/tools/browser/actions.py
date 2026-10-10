"""Command handlers: each action runs act → settle → snapshot → response."""

import json
import re

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import BrowserError
from .protocol import DEFAULT_TEXT_MAX, DEFAULT_TIMEOUT_S, SETTLE_CAP_S
from .session import SnapState
from .settle import doc_id, settle, wait_for_condition
from .snapshot import clean_raw, diff, render_diff, render_tree
from .urlpolicy import check_scheme

REF_RE = re.compile(r"^e\d+$")
SNAPSHOT_MODES = frozenset({"diff", "full", "none"})
WAIT_UNTIL = frozenset({"domcontentloaded", "load", "networkidle"})


def _timeout_s(req: dict) -> float:
    return float(req.get("timeout", DEFAULT_TIMEOUT_S))


def _ms(req: dict) -> float:
    return _timeout_s(req) * 1000


def _first_line(e: Exception) -> str:
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__


def _require(req: dict, key: str, allow_empty: bool = False):
    value = req.get(key)
    if value is None or (value == "" and not allow_empty):
        raise BrowserError(f"{req.get('cmd')}: missing required argument '{key}'", "usage_error")
    return value


def _stale(ref: str) -> BrowserError:
    return BrowserError(f"stale ref {ref} — run snapshot", "stale_ref")


class Actions:
    def __init__(self, session):
        self.s = session
        self._shots = 0
        self._handlers = {
            "open": self._open, "snapshot": self._snapshot, "click": self._click, "fill": self._fill,
            "select": self._select, "press": self._press, "type": self._type, "wait": self._wait,
            "text": self._text, "screenshot": self._screenshot, "back": self._back, "tabs": self._tabs,
            "eval": self._eval, "run": self._run,
        }

    async def execute(self, req: dict) -> dict:
        handler = self._handlers.get(req.get("cmd"))
        if handler is None:
            raise BrowserError(f"unknown command {req.get('cmd')!r}", "usage_error")
        if req.get("snapshot", "diff") not in SNAPSHOT_MODES:
            raise BrowserError("--snapshot must be one of diff, full, none", "usage_error")
        self.s.begin_command(accept_dialog=bool(req.get("accept_dialog")))
        try:
            return await handler(req)
        except PlaywrightTimeoutError as e:
            raise BrowserError(f"timeout after {_timeout_s(req):g}s: {_first_line(e)}", "timeout") from e
        except PlaywrightError as e:
            raise BrowserError(_first_line(e), "browser_error") from e

    # --- pipeline -----------------------------------------------------------

    async def _resolve(self, page, ref: str):
        if not REF_RE.match(ref or ""):
            raise BrowserError(f"invalid ref {ref!r} (expected e.g. e14)", "usage_error")
        snap = self.s.last.get(page)
        if snap is None or snap.doc is None or await doc_id(page) != snap.doc:
            raise _stale(ref)
        pw_ref = snap.refs.get(ref)
        if pw_ref is None:
            raise _stale(ref)
        locator = page.locator(f"aria-ref={pw_ref}")
        try:
            count = await locator.count()
        except PlaywrightError:
            count = 0
        if count != 1:
            raise _stale(ref)
        return locator

    async def _after(self, req: dict, page, doc_before, force_full: bool = False) -> dict:
        settled, navigated = False, False
        tracker = self.s.trackers.get(page)
        if tracker is not None:  # the action may have closed the tab
            settled, navigated = await settle(page, tracker, doc_before)
        # A popup's "page" event can arrive after click() returns; by now (>= QUIET_S later) it has.
        target = await self.s.ensure_page()
        new_tab = target is not page
        if new_tab:
            settled, _ = await settle(target, self.s.trackers[target], None)
        if req.get("wait_for") or req.get("wait_for_selector"):
            await wait_for_condition(target, req.get("wait_for"), req.get("wait_for_selector"), _timeout_s(req))
        await self.s.drain_downloads(SETTLE_CAP_S)
        return await self._respond(req, target, settled, navigated or new_tab, force_full or navigated or new_tab)

    async def _respond(self, req: dict, page, settled: bool, navigated: bool, full: bool) -> dict:
        _, prev, cur = await self.s.snapshot(page)
        mode = req.get("snapshot", "diff")
        if mode == "none":
            body = ""
        elif mode == "full" or full or prev is None:
            body = render_tree(cur.nodes) or "(no interactive elements)"
        else:
            d = diff(prev.nodes, cur.nodes)
            body = render_tree(cur.nodes) if d.too_large() else render_diff(d)
        if req.get("text"):
            text = await self._page_text(page, None, int(req.get("max", DEFAULT_TEXT_MAX)))
            body = (body + "\n\n" if body else "") + "--- page text ---\n" + text
        return {"ok": True, "header": await self.s.header(page, settled, navigated), "body": body}

    async def _info(self, page, body: str, **extra) -> dict:
        header = await self.s.header(page, True, False)
        header.update(extra)
        return {"ok": True, "header": header, "body": body}

    # --- commands -------------------------------------------------------------

    async def _open(self, req: dict) -> dict:
        url = _require(req, "url")
        check_scheme(url)
        wait = req.get("wait", "domcontentloaded")
        if wait not in WAIT_UNTIL:
            raise BrowserError("--wait must be one of domcontentloaded, load, networkidle", "usage_error")
        page = await self.s.ensure_page()
        try:
            await page.goto(url, wait_until=wait, timeout=_ms(req))
        except PlaywrightError as e:
            if self.s.events["blocked"]:
                hosts = ", ".join(self.s.events["blocked"])
                raise BrowserError(
                    f"blocked private-network address ({hosts}) — start the session with --allow-private to allow",
                    "policy_blocked",
                ) from e
            if isinstance(e, PlaywrightTimeoutError):
                raise
            raise BrowserError(f"navigation failed: {_first_line(e)}", "nav_error") from e
        return await self._after(req, page, doc_before=None, force_full=True)

    async def _snapshot(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        raw, _, cur = await self.s.snapshot(page)
        body = clean_raw(raw) if req.get("all") else (render_tree(cur.nodes) or "(no interactive elements)")
        return await self._info(page, body)

    async def _click(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        locator = await self._resolve(page, _require(req, "ref"))
        before = await doc_id(page)
        await locator.click(timeout=_ms(req))
        return await self._after(req, page, before)

    async def _fill(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        locator = await self._resolve(page, _require(req, "ref"))
        before = await doc_id(page)
        await locator.fill(str(_require(req, "value", allow_empty=True)), timeout=_ms(req))
        return await self._after(req, page, before)

    async def _select(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        locator = await self._resolve(page, _require(req, "ref"))
        before = await doc_id(page)
        await locator.select_option(str(_require(req, "value")), timeout=_ms(req))
        return await self._after(req, page, before)

    async def _press(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        key = _require(req, "key")
        locator = await self._resolve(page, req["ref"]) if req.get("ref") else None
        before = await doc_id(page)
        if locator is not None:
            await locator.press(key, timeout=_ms(req))
        else:
            await page.keyboard.press(key)
        return await self._after(req, page, before)

    async def _type(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        locator = await self._resolve(page, _require(req, "ref"))
        text = str(_require(req, "value"))
        before = await doc_id(page)
        baseline = self.s.last[page]
        await locator.fill("", timeout=_ms(req))
        await locator.press_sequentially(text, delay=50, timeout=_ms(req))
        if req.get("pick") is None:
            return await self._after(req, page, before)
        pick = int(req["pick"])
        await settle(page, self.s.trackers[page], before)
        _, _, cur = await self.s.snapshot(page)
        options = [n for n in diff(baseline.nodes, cur.nodes).added if n.role == "option" and n.ref]
        if not options:
            raise BrowserError("no autocomplete options appeared", "no_options")
        if not 1 <= pick <= len(options):
            raise BrowserError(f"--pick {pick} is out of range: {len(options)} options appeared", "usage_error")
        await page.locator(f"aria-ref={cur.refs[options[pick - 1].ref]}").click(timeout=_ms(req))
        # Report the combined change relative to the state before typing.
        self.s.last[page] = SnapState(cur.doc, baseline.nodes, cur.refs)
        return await self._after(req, page, before)

    async def _wait(self, req: dict) -> dict:
        if not (req.get("wait_for") or req.get("wait_for_selector")):
            raise BrowserError("wait needs --text or --selector", "usage_error")
        page = await self.s.ensure_page()
        return await self._after(req, page, await doc_id(page))

    async def _text(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        body = await self._page_text(page, req.get("selector"), int(req.get("max", DEFAULT_TEXT_MAX)))
        return await self._info(page, body)

    async def _page_text(self, page, selector, max_chars: int) -> str:
        for candidate in [selector] if selector else ["main", "body"]:
            locator = page.locator(candidate).first
            if await locator.count():
                text = await locator.inner_text(timeout=5000)
                break
        else:
            if selector:
                raise BrowserError(f"no element matches selector {selector!r}", "not_found")
            text = ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n[truncated {len(text) - max_chars} chars]"
        return text

    async def _screenshot(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        self._shots += 1
        path = self.s.artifacts / f"screenshot-{self._shots}.png"
        if req.get("ref"):
            locator = await self._resolve(page, req["ref"])
            await locator.screenshot(path=str(path), timeout=_ms(req))
        else:
            await page.screenshot(path=str(path), full_page=bool(req.get("full_page")), timeout=_ms(req))
        return await self._info(page, "", screenshot=str(path))

    async def _back(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        before = await doc_id(page)
        response = await page.go_back(wait_until="domcontentloaded", timeout=_ms(req))
        if response is None and await doc_id(page) == before:
            raise BrowserError("no previous page in this tab's history", "nav_error")
        return await self._after(req, page, before, force_full=True)

    async def _tabs(self, req: dict) -> dict:
        if req.get("switch") is not None:
            index = int(req["switch"]) - 1
            if not 0 <= index < len(self.s.pages):
                raise BrowserError(f"no tab {index + 1}: {len(self.s.pages)} tabs open", "usage_error")
            self.s.current = index
            page = self.s.page
            await page.bring_to_front()
            return await self._respond(req, page, True, False, full=True)
        lines = []
        for i, p in enumerate(self.s.pages):
            marker = "*" if i == self.s.current else " "
            lines.append(f"{marker} {i + 1}. {await p.title()} — {p.url}")
        return await self._info(self.s.page, "\n".join(lines))

    async def _eval(self, req: dict) -> dict:
        page = await self.s.ensure_page()
        try:
            result = await page.evaluate(_require(req, "js"))
        except PlaywrightError as e:
            raise BrowserError("eval threw an exception (message omitted: page-controlled)", "eval_error",
                               {"untrusted": _first_line(e)}) from e
        return await self._info(page, json.dumps(result, ensure_ascii=False, default=str, indent=1))

    async def _run(self, req: dict) -> dict:
        steps = req.get("steps")
        if not isinstance(steps, list) or not steps:
            raise BrowserError("run needs a non-empty JSON array of steps", "usage_error")
        completed, resp = [], None
        for i, step in enumerate(steps, 1):
            if not isinstance(step, dict) or step.get("cmd") in (None, "run"):
                raise BrowserError(f"step {i}: each step must be an object with a 'cmd' (not 'run')", "usage_error")
            step = dict(step)
            step.setdefault("snapshot", "none" if i < len(steps) else req.get("snapshot", "diff"))
            try:
                resp = await self.execute(step)
            except BrowserError as e:
                details = {"completed": completed}
                if e.details and "untrusted" in e.details:
                    details["untrusted"] = e.details["untrusted"]
                raise BrowserError(f"step {i} ({step['cmd']}) failed: {e.message}", e.code, details) from e
            # No url here: steps also land in error details on stderr, which must stay page-text free.
            completed.append({"step": i, "cmd": step["cmd"], "settled": resp["header"].get("settled")})
        resp["header"]["steps"] = completed
        return resp
