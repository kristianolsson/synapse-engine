"""URL policy: scheme allowlist and private-network blocking.

Prevents the browser from reading local files and from reaching the LAN
(NAS admin UI, router, other home services) when steered by a hostile page.
"""

import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

from .errors import BrowserError

ALLOWED_SCHEMES = frozenset({"http", "https"})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def check_scheme(url: str) -> None:
    """Raise policy_blocked unless the URL is http(s) or about:blank."""
    if url == "about:blank":
        return
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BrowserError(
            f"blocked URL scheme '{scheme or '(none)'}' — only http, https and about:blank are allowed",
            "policy_blocked",
        )


def is_private_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
        or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT)
    )


def is_private_name(host: str) -> bool:
    h = host.lower().rstrip(".")
    return h == "localhost" or h.endswith(".localhost") or h.endswith(".local")


async def _default_resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return [info[4][0] for info in infos]


class PrivateNetworkGuard:
    """Decides (with a per-host cache) whether a host resolves to a private address."""

    def __init__(self, resolve: Optional[Callable[[str], Awaitable[list[str]]]] = None):
        self._resolve = resolve or _default_resolve
        self._cache: dict[str, bool] = {}

    async def blocked(self, host: str) -> bool:
        if host in self._cache:
            return self._cache[host]
        if is_private_name(host):
            result = True
        else:
            try:
                result = is_private_ip(host.strip("[]"))
            except ValueError:
                # Unresolvable hosts are not blocked: the browser reports the DNS failure itself.
                result = any(is_private_ip(ip) for ip in await self._resolve(host))
        self._cache[host] = result
        return result
