"""Live Playwright state for one browser session: context, tabs, events, snapshots."""

import asyncio
import logging
from dataclasses import dataclass
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
            )
        except Exception:
            await self._pw.stop()
            raise
        await self.context.add_init_script(INIT_SCRIPT)
        if self._guard:
            await self.context.route("**/*", self._route)
        self.context.on("page", self._on_new_page)
        self.context.on("close", lambda _ctx: self.closed.set())
        for page in self.context.pages:
            self._attach(page)
        if not self.pages:
            self._attach(await self.context.new_page())
        self.current = 0
        self.events = _empty_events()

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
            self.pages.remove(page)
            self.trackers.pop(page, None)
            self.last.pop(page, None)
            self.current = min(self.current, max(0, len(self.pages) - 1))

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

    async def _route(self, route) -> None:
        host = urlsplit(route.request.url).hostname
        if host and await self._guard.blocked(host):
            if host not in self.events["blocked"]:
                self.events["blocked"].append(host)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def snapshot(self, page) -> tuple:
        raw = await page.aria_snapshot(mode="ai")
        state = SnapState(doc=await doc_id(page), nodes=filter_interactive(parse(raw)))
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
