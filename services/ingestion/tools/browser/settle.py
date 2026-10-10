"""Settle detection: wait until a page stops changing after an action."""

import asyncio
import time
from typing import Optional

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import BrowserError
from .protocol import LONG_REQUEST_S, QUIET_S, SETTLE_CAP_S

# Runs at the start of every document and frame. Math.random rather than
# crypto.randomUUID, which is undefined on non-secure (plain http) pages.
INIT_SCRIPT = """
(() => {
  if (window.__synapseDoc) return;
  window.__synapseDoc = Math.random().toString(36).slice(2) + Date.now().toString(36);
  window.__synapseLastMutation = performance.now();
  new MutationObserver(() => { window.__synapseLastMutation = performance.now(); })
    .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
})();
"""
_PROBE_JS = "() => window.__synapseDoc ? [window.__synapseDoc, performance.now() - window.__synapseLastMutation] : null"
_DOC_JS = "() => window.__synapseDoc || null"
_IGNORED_RESOURCE_TYPES = frozenset({"eventsource", "websocket"})


class NetworkTracker:
    """Counts in-flight requests for one page (ignoring streams and long-polls)."""

    def __init__(self, page, clock=time.monotonic):
        self._clock = clock
        self._inflight: dict = {}
        self.last_activity = clock()
        page.on("request", self._on_start)
        page.on("requestfinished", self._on_end)
        page.on("requestfailed", self._on_end)

    def _on_start(self, request) -> None:
        if request.resource_type in _IGNORED_RESOURCE_TYPES:
            return
        now = self._clock()
        self._inflight[request] = now
        self.last_activity = now

    def _on_end(self, request) -> None:
        if self._inflight.pop(request, None) is not None:
            self.last_activity = self._clock()

    def busy(self) -> bool:
        now = self._clock()
        if any(now - started < LONG_REQUEST_S for started in self._inflight.values()):
            return True
        return now - self.last_activity < QUIET_S


async def _evaluate(page, js: str):
    try:
        return await asyncio.wait_for(page.evaluate(js), 1.0)
    except (PlaywrightError, asyncio.TimeoutError):
        return None


async def doc_id(page) -> Optional[str]:
    return await _evaluate(page, _DOC_JS)


async def settle(page, tracker: NetworkTracker, doc_before: Optional[str], cap: float = SETTLE_CAP_S) -> tuple:
    """Wait for navigation load or a quiet page, up to `cap` seconds. Returns (settled, navigated)."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + cap
    navigated = False
    while True:
        probe = await _evaluate(page, _PROBE_JS)
        if probe is None and page.url == "about:blank":
            return True, navigated  # init scripts don't run on about:blank; nothing to wait for
        if probe is not None:
            doc, since_mutation_ms = probe
            if doc != doc_before and not navigated:
                navigated = True
                remaining = deadline - loop.time()
                if remaining > 0:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=remaining * 1000)
                    except PlaywrightError:
                        pass
                continue
            quiet = since_mutation_ms >= QUIET_S * 1000 and not tracker.busy()
            if quiet and loop.time() - start >= QUIET_S:
                return True, navigated
        if loop.time() >= deadline:
            return False, navigated
        await asyncio.sleep(0.1)


async def wait_for_condition(page, text: Optional[str], selector: Optional[str], timeout_s: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    what = ""
    try:
        if text:
            what = f"text {text!r}"
            await page.get_by_text(text).filter(visible=True).first.wait_for(
                state="visible", timeout=max(0.0, deadline - loop.time()) * 1000)
        if selector:
            what = f"selector {selector!r}"
            await page.wait_for_selector(
                selector, state="visible", timeout=max(0.0, deadline - loop.time()) * 1000)
    except PlaywrightTimeoutError as e:
        raise BrowserError(f"timeout after {timeout_s:g}s waiting for {what}", "timeout") from e
