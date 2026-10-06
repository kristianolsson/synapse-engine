"""Unit tests for the policy-enforcing forward proxy (no browser)."""

import asyncio

import pytest
import pytest_asyncio

from services.ingestion.tools.browser.netproxy import MAX_HEADER_BYTES, NetProxy


class FakeGuard:
    """Maps host -> checked addresses (None = blocked); records every lookup."""

    def __init__(self, table, error=False):
        self.table = table
        self.error = error
        self.calls = []

    async def addresses(self, host):
        self.calls.append(host)
        if self.error:
            raise RuntimeError("resolver exploded")
        return self.table.get(host, [])


class Upstream:
    """Local TCP server: echoes raw bytes, or answers one HTTP request when http=True."""

    def __init__(self, http=False):
        self.http = http
        self.connections = 0
        self.received = b""

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            if self.http:
                self.received = await reader.readuntil(b"\r\n\r\n")
                body = b"hello"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nKeep-Alive: timeout=5\r\n"
                             b"Connection: keep-alive\r\n\r\n" + body)
                await writer.drain()  # then close, as the proxy asked with Connection: close
            else:
                while data := await reader.read(1024):
                    self.received += data
                    writer.write(data)
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()


@pytest_asyncio.fixture
async def make_proxy():
    proxies = []

    async def make(guard):
        blocked = []
        proxy = NetProxy(guard, blocked.append)
        await proxy.start()
        proxies.append(proxy)
        return proxy, blocked

    yield make
    for proxy in proxies:
        await proxy.close()


async def _send(proxy, data: bytes):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(data)
    await writer.drain()
    return reader, writer


async def _read_all(reader) -> bytes:
    return await asyncio.wait_for(reader.read(), 5)


async def test_connect_to_allowed_host_tunnels_bytes(make_proxy):
    async with Upstream() as up:
        proxy, blocked = await make_proxy(FakeGuard({"allowed.test": ["127.0.0.1"]}))
        reader, writer = await _send(proxy, f"CONNECT allowed.test:{up.port} HTTP/1.1\r\nHost: allowed.test\r\n\r\n".encode())
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert status.startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), 5) == b"ping"
        writer.close()
        assert blocked == []


async def test_connect_to_blocked_host_is_refused_and_reported(make_proxy):
    async with Upstream() as up:
        proxy, blocked = await make_proxy(FakeGuard({"nas.test": None}))
        reader, writer = await _send(proxy, f"CONNECT nas.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        assert await _read_all(reader) == b""  # closed without a response
        writer.close()
        reader, writer = await _send(proxy, f"CONNECT nas.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        assert await _read_all(reader) == b""
        writer.close()
        assert blocked == ["nas.test", "nas.test"]  # the session dedupes
        assert up.connections == 0


async def test_absolute_form_to_blocked_host_is_refused_and_reported(make_proxy):
    async with Upstream(http=True) as up:
        proxy, blocked = await make_proxy(FakeGuard({"router.test": None}))
        reader, writer = await _send(proxy, f"GET http://router.test:{up.port}/admin HTTP/1.1\r\nHost: router.test\r\n\r\n".encode())
        assert await _read_all(reader) == b""
        writer.close()
        assert blocked == ["router.test"]
        assert up.connections == 0


async def test_absolute_form_to_allowed_host_is_forwarded_in_origin_form(make_proxy):
    async with Upstream(http=True) as up:
        proxy, blocked = await make_proxy(FakeGuard({"site.test": ["127.0.0.1"]}))
        reader, writer = await _send(
            proxy,
            f"GET http://site.test:{up.port}/a?b=1 HTTP/1.1\r\nHost: site.test:{up.port}\r\n"
            "Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n".encode(),
        )
        response = await _read_all(reader)
        writer.close()
        request = up.received.decode()
        assert request.startswith("GET /a?b=1 HTTP/1.1\r\n")
        assert f"Host: site.test:{up.port}\r\n" in request
        assert "Proxy-Connection" not in request and "Connection: close\r\n" in request
        assert response.startswith(b"HTTP/1.1 200 OK\r\n") and response.endswith(b"\r\n\r\nhello")
        assert b"Connection: close\r\n" in response and b"keep-alive" not in response.lower()
        assert blocked == []


async def test_guard_exception_refuses(make_proxy):
    async with Upstream() as up:
        proxy, blocked = await make_proxy(FakeGuard({}, error=True))
        reader, writer = await _send(proxy, f"CONNECT any.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        assert await _read_all(reader) == b""
        writer.close()
        assert up.connections == 0


async def test_unresolvable_host_refused_but_not_reported(make_proxy):
    proxy, blocked = await make_proxy(FakeGuard({"nxdomain.test": []}))
    reader, writer = await _send(proxy, b"CONNECT nxdomain.test:443 HTTP/1.1\r\n\r\n")
    assert await _read_all(reader) == b""
    writer.close()
    assert blocked == []


async def test_connects_to_checked_ip_not_a_fresh_lookup(make_proxy):
    # "pinned.test" has no real DNS record: the tunnel only works if the proxy dials the
    # address the guard approved instead of resolving the name again.
    async with Upstream() as up:
        guard = FakeGuard({"pinned.test": ["127.0.0.1"]})
        proxy, _ = await make_proxy(guard)
        reader, writer = await _send(proxy, f"CONNECT pinned.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).startswith(b"HTTP/1.1 200")
        writer.write(b"x")
        assert await asyncio.wait_for(reader.readexactly(1), 5) == b"x"
        writer.close()
        assert guard.calls == ["pinned.test"]
        assert up.connections == 1


async def test_oversized_header_is_refused(make_proxy):
    guard = FakeGuard({"site.test": ["127.0.0.1"]})
    proxy, _ = await make_proxy(guard)
    reader, writer = await _send(proxy, b"GET http://site.test/ HTTP/1.1\r\nX: " + b"a" * (MAX_HEADER_BYTES + 10))
    assert await _read_all(reader) == b""
    writer.close()
    assert guard.calls == []


async def test_close_tears_down_open_tunnels(make_proxy):
    async with Upstream() as up:
        proxy, _ = await make_proxy(FakeGuard({"allowed.test": ["127.0.0.1"]}))
        reader, writer = await _send(proxy, f"CONNECT allowed.test:{up.port} HTTP/1.1\r\n\r\n".encode())
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        await asyncio.wait_for(proxy.close(), 5)
        assert await _read_all(reader) == b""
        assert not proxy._tasks
        writer.close()
