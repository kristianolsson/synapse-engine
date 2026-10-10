"""Daemon-local HTTP forward proxy that enforces the private-network policy on every connection.

Firefox is pointed at this proxy, so every connection it opens (navigations, each hop of a
redirect chain, subresources, WebSockets, worker fetches) arrives here, either as CONNECT
(https, wss) or as an absolute-form request (plain http). For each one the proxy asks the guard
for the host's checked addresses and dials one of those addresses itself (DNS pinning: no second
lookup can swap in a LAN address). Refused connections are closed without an HTTP response, so
Firefox fails the load with a network error instead of rendering a proxy-generated page.
"""

import asyncio
import contextlib
import logging
from typing import Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("browser.netproxy")

MAX_HEADER_BYTES = 64 * 1024  # request head from Firefox
MAX_RESPONSE_HEADER_BYTES = 256 * 1024  # response head from the web (large Set-Cookie headers)
HEADER_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 15.0
_CHUNK = 64 * 1024
_END = b"\r\n\r\n"
_PROXY_ONLY = {b"proxy-connection", b"proxy-authorization"}
_CONNECTION = {b"connection", b"keep-alive"}


def _header_name(line: bytes) -> bytes:
    return line.split(b":", 1)[0].strip().lower()


def _close_after(head: bytes) -> bytes:
    """Force Connection: close on a response head so Firefox never reuses the proxy connection
    for another host (each proxy connection is checked once, for one host)."""
    lines = head[: -len(_END)].split(b"\r\n")
    status = lines[0].split(b" ", 2)
    if len(status) >= 2 and status[1].startswith(b"1"):
        return head  # interim response; leave untouched
    kept = [line for line in lines[1:] if _header_name(line) not in _CONNECTION | _PROXY_ONLY]
    return b"\r\n".join([lines[0], *kept, b"Connection: close"]) + _END


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, rewrite_head: bool = False) -> None:
    if rewrite_head:
        try:
            head = _close_after(await src.readuntil(_END))
        except asyncio.IncompleteReadError as e:
            head = e.partial
        dst.write(head)
        await dst.drain()
    while data := await src.read(_CHUNK):
        dst.write(data)
        await dst.drain()


async def _relay(client, upstream, rewrite_response: bool = False) -> None:
    """Stream bytes both ways until either side finishes, then stop the other direction."""
    (c_reader, c_writer), (u_reader, u_writer) = client, upstream
    tasks = {
        asyncio.create_task(_pump(c_reader, u_writer)),
        asyncio.create_task(_pump(u_reader, c_writer, rewrite_response)),
    }
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class NetProxy:
    """Forward proxy on 127.0.0.1:<ephemeral>. `guard.addresses(host)` returns the checked
    addresses (None = blocked, [] = unresolvable); `on_blocked(host)` is told about refusals."""

    def __init__(self, guard, on_blocked: Callable[[str], None]):
        self._guard = guard
        self._on_blocked = on_blocked
        self._server: Optional[asyncio.Server] = None
        self._tasks: set = set()
        self.port: Optional[int] = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=MAX_HEADER_BYTES)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        upstream = None
        try:
            head = await asyncio.wait_for(reader.readuntil(_END), HEADER_TIMEOUT_S)
            lines = head[: -len(_END)].split(b"\r\n")
            method, target, version = lines[0].decode("latin-1").split(" ")
            if method == "CONNECT":
                url = urlsplit("//" + target)
                if not url.hostname or url.port is None:
                    return
                upstream = await self._dial(url.hostname, url.port)
                if upstream is None:
                    return
                writer.write(b"HTTP/1.1 200 Connection established" + _END)
                await writer.drain()
                await _relay((reader, writer), upstream)
                return
            url = urlsplit(target)
            if url.scheme != "http" or not url.hostname:
                return
            upstream = await self._dial(url.hostname, url.port or 80)
            if upstream is None:
                return
            headers = [line for line in lines[1:] if _header_name(line) not in _PROXY_ONLY]
            upgrade = any(_header_name(line) == b"upgrade" for line in headers)
            if not upgrade:  # plain request: one request per proxy connection
                headers = [line for line in headers if _header_name(line) not in _CONNECTION]
                headers.append(b"Connection: close")
            if not any(_header_name(line) == b"host" for line in headers):
                headers.insert(0, b"Host: " + url.netloc.encode("latin-1"))
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            request_line = f"{method} {path} {version}".encode("latin-1")
            upstream[1].write(b"\r\n".join([request_line, *headers]) + _END)
            await _relay((reader, writer), upstream, rewrite_response=not upgrade)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ValueError, UnicodeError, ConnectionError, OSError):
            pass  # malformed, oversized, slow or dropped: just hang up
        except Exception:
            logger.warning("proxy connection failed", exc_info=True)
        finally:
            for w in (writer, upstream[1] if upstream else None):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()
            self._tasks.discard(task)

    async def _dial(self, host: str, port: int):
        """Connect to one of the host's checked addresses, or return None (refused)."""
        try:
            addresses = await self._guard.addresses(host)
        except Exception:  # fail closed
            logger.warning("private-network check failed; refusing connection", exc_info=True)
            return None
        if addresses is None:
            try:
                self._on_blocked(host)
            except Exception:
                logger.warning("blocked-host callback failed", exc_info=True)
            return None
        for ip in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(ip, port, limit=MAX_RESPONSE_HEADER_BYTES), CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError):
                continue
        return None
