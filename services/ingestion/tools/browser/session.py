"""Live Playwright state for one browser session: context, tabs, events, snapshots."""

import asyncio
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .settle import INIT_SCRIPT, NetworkTracker, doc_id
from .snapshot import filter_interactive, parse
from .urlpolicy import PrivateNetworkGuard

logger = logging.getLogger("browser.session")


@dataclass
class SnapState:
    doc: Optional[str]
    nodes: list
    refs: dict = field(default_factory=dict)  # display ref -> Playwright ref


_RAW_REF_RE = re.compile(r"\[ref=([^\]]+)\]")


def _empty_events() -> dict:
    return {"dialogs": [], "downloads": [], "blocked": [], "new_tab": False}


class Session:
    def __init__(self, profile: Path, artifacts: Path, headed: bool = False, allow_private: bool = False):
        self.profile = profile
        self.artifacts = artifacts
        self.headed = headed
        self.pages: list = []
        self.current = 0
        self.trackers: dict = {}
        self.last: dict = {}
        self._ref_counter = 0
        self._ref_maps: dict = {}  # (page, doc) -> {pw_ref: display_ref}
        self.events = _empty_events()
        self.accept_dialog = False
        self.closed = asyncio.Event()
        self._guard = None if allow_private else PrivateNetworkGuard()
        self._pending_downloads: set = set()
        self._pw = None
        self.context = None

    async def start(self) -> None:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            self.context = await self._pw.firefox.launch_persistent_context(
                str(self.profile),
                headless=not self.headed,
                viewport={"width": 1280, "height": 900},
                accept_downloads=True,
                service_workers="block",
            )
        except Exception:
            await self._pw.stop()
            raise
        try:
            await self.context.add_init_script(INIT_SCRIPT)
            if self._guard:
                await self.context.route("**/*", self._route)
                await self.context.route_web_socket("**/*", self._route_ws)
            self.context.on("page", self._on_new_page)
            self.context.on("close", lambda _ctx: self.closed.set())
            # Pages that predate route_web_socket() are not covered by it, so replace them.
            stale = list(self.context.pages)
            self._attach(await self.context.new_page())
            for page in stale:
                await page.close()
            self.current = 0
            self.events = _empty_events()
        except BaseException:
            try:
                await self.close()
            except Exception:
                logger.warning("cleanup after failed start also failed", exc_info=True)
            raise

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
        finally:
            if self._pw:
                await self._pw.stop()

    @property
    def page(self):
        return self.pages[self.current]

    async def ensure_page(self):
        if not self.pages:
            self._attach(await self.context.new_page())
            self.current = 0
        return self.page

    def begin_command(self, accept_dialog: bool) -> None:
        self.events = _empty_events()
        self.accept_dialog = accept_dialog

    def _attach(self, page) -> bool:
        if page in self.trackers:
            return False
        self.trackers[page] = NetworkTracker(page)
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)
        page.on("close", self._on_close)
        self.pages.append(page)
        return True

    def _on_new_page(self, page) -> None:
        if self._attach(page):
            self.current = self.pages.index(page)
            self.events["new_tab"] = True

    def _on_close(self, page) -> None:
        if page in self.pages:
            was_current = self.pages[self.current] is page
            current_page = None if was_current else self.pages[self.current]
            self.pages.remove(page)
            self.trackers.pop(page, None)
            self.last.pop(page, None)
            for key in [k for k in self._ref_maps if k[0] is page]:
                del self._ref_maps[key]
            if current_page is not None:
                self.current = self.pages.index(current_page)
            else:
                self.current = max(0, len(self.pages) - 1)

    async def _on_dialog(self, dialog) -> None:
        action = "accepted" if self.accept_dialog else "dismissed"
        self.events["dialogs"].append({"type": dialog.type, "message": dialog.message, "action": action})
        try:
            if self.accept_dialog:
                await dialog.accept()
            else:
                await dialog.dismiss()
        except PlaywrightError:
            pass

    def _on_download(self, download) -> None:
        task = asyncio.ensure_future(self._save_download(download))
        self._pending_downloads.add(task)
        task.add_done_callback(self._pending_downloads.discard)

    async def _save_download(self, download) -> None:
        target = self.artifacts / Path(download.suggested_filename).name
        try:
            await download.save_as(target)
            self.events["downloads"].append(str(target))
        except PlaywrightError as e:
            logger.warning("download failed: %s", e)

    async def drain_downloads(self, timeout: float) -> None:
        if self._pending_downloads:
            await asyncio.wait(set(self._pending_downloads), timeout=timeout)

    def _record_blocked(self, host: str) -> None:
        if host not in self.events["blocked"]:
            self.events["blocked"].append(host)

    async def _route(self, route) -> None:
        host = urlsplit(route.request.url).hostname
        try:
            blocked = bool(host) and await self._guard.blocked(host)
        except Exception:  # fail closed
            await route.abort("blockedbyclient")
            return
        if blocked:
            self._record_blocked(host)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def _route_ws(self, ws_route) -> None:
        host = urlsplit(ws_route.url).hostname
        try:
            blocked = bool(host) and await self._guard.blocked(host)
        except Exception:  # fail closed
            blocked = True
        if blocked:
            if host:
                self._record_blocked(host)
            await ws_route.close()
        else:
            ws_route.connect_to_server()

    def _display_ref(self, page, doc, pw_ref: str) -> str:
        """Session-unique ref: never reused across documents or tabs, stable within a document."""
        refs = self._ref_maps.setdefault((page, doc), {})
        if pw_ref not in refs:
            self._ref_counter += 1
            refs[pw_ref] = f"e{self._ref_counter}"
        return refs[pw_ref]

    def _remap(self, page, doc, nodes: list) -> list:
        return [
            replace(n, ref=self._display_ref(page, doc, n.ref) if n.ref else None,
                    children=self._remap(page, doc, n.children))
            for n in nodes
        ]

    async def snapshot(self, page) -> tuple:
        # doc id first: if a navigation lands mid-snapshot, the old refs are filed under the
        # old doc id and resolve as stale_ref instead of pointing into the new document.
        doc = await doc_id(page)
        raw = await page.aria_snapshot(mode="ai")
        nodes = self._remap(page, doc, filter_interactive(parse(raw)))
        raw = _RAW_REF_RE.sub(lambda m: f"[ref={self._display_ref(page, doc, m.group(1))}]", raw)
        reverse = {d: pw for pw, d in self._ref_maps.get((page, doc), {}).items()}
        state = SnapState(doc=doc, nodes=nodes, refs=reverse)
        prev = self.last.get(page)
        self.last[page] = state
        return raw, prev, state

    async def header(self, page, settled: bool, navigated: bool) -> dict:
        try:
            title = await page.title()
        except PlaywrightError:
            title = ""
        header = {
            "url": page.url,
            "title": title,
            "settled": settled,
            "navigated": navigated,
            "tab": self.pages.index(page) + 1 if page in self.pages else None,
            "tabs": len(self.pages),
        }
        header.update({k: v for k, v in self.events.items() if v})
        return header
