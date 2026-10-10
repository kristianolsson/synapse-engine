"""Unit tests for the browser URL policy."""

import pytest

from services.ingestion.tools.browser import protocol
from services.ingestion.tools.browser.errors import BrowserError
from services.ingestion.tools.browser.urlpolicy import (
    PrivateNetworkGuard,
    check_scheme,
    is_private_ip,
    is_private_name,
)


@pytest.mark.parametrize("url", ["http://example.com", "https://example.com/x?y=1", "about:blank"])
def test_check_scheme_allows(url):
    check_scheme(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "about:config", "ftp://x", "example.com"])
def test_check_scheme_blocks(url):
    with pytest.raises(BrowserError) as e:
        check_scheme(url)
    assert e.value.code == "policy_blocked"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.10", "169.254.1.1", "100.64.0.1", "::1", "fe80::1%en0", "::ffff:192.168.1.1", "0.0.0.0"])
def test_private_ips(ip):
    assert is_private_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "142.250.72.14", "2607:f8b0:4005:80a::200e"])
def test_public_ips(ip):
    assert not is_private_ip(ip)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "foo.localhost", "nas.local"])
def test_private_names(host):
    assert is_private_name(host)


def test_public_name():
    assert not is_private_name("www.google.com")


async def test_guard_returns_checked_addresses_and_caches():
    calls = []

    async def resolve(host):
        calls.append(host)
        return {"intranet.example": ["192.168.1.5"],
                "www.example.com": ["93.184.216.34", "93.184.216.34", "2606:2800::1"]}[host]

    guard = PrivateNetworkGuard(resolve=resolve)
    assert await guard.addresses("intranet.example") is None
    assert await guard.addresses("www.example.com") == ["93.184.216.34", "2606:2800::1"]
    assert await guard.addresses("intranet.example") is None
    assert calls == ["intranet.example", "www.example.com"]


async def test_guard_blocks_if_any_address_is_private():
    async def resolve(host):
        return ["93.184.216.34", "10.0.0.1"]

    assert await PrivateNetworkGuard(resolve=resolve).addresses("mixed.example") is None


async def test_guard_pins_first_answer_against_rebinding():
    answers = [["93.184.216.34"], ["192.168.1.1"]]

    async def resolve(host):
        return answers.pop(0)

    guard = PrivateNetworkGuard(resolve=resolve)
    assert await guard.addresses("rebind.example") == ["93.184.216.34"]
    assert await guard.addresses("rebind.example") == ["93.184.216.34"]


async def test_guard_literal_and_names_skip_resolver():
    async def resolve(host):
        raise AssertionError("should not resolve")

    guard = PrivateNetworkGuard(resolve=resolve)
    assert await guard.addresses("127.0.0.1") is None
    assert await guard.addresses("[::1]") is None
    assert await guard.addresses("localhost") is None
    assert await guard.addresses("8.8.8.8") == ["8.8.8.8"]


async def test_guard_unresolvable_host_has_no_addresses_and_is_not_cached():
    answers = [[], ["93.184.216.34"]]

    async def resolve(host):
        return answers.pop(0)

    guard = PrivateNetworkGuard(resolve=resolve)
    assert await guard.addresses("flaky.example") == []
    assert await guard.addresses("flaky.example") == ["93.184.216.34"]


def test_command_deadline():
    t, cap, slack = protocol.DEFAULT_TIMEOUT_S, protocol.SETTLE_CAP_S, protocol.DAEMON_SLACK_S
    assert protocol.command_deadline({"cmd": "click"}) == 2 * t + cap + slack
    assert protocol.command_deadline({"cmd": "click", "timeout": 2}) == 4 + cap + slack
    run = {"cmd": "run", "steps": [{"cmd": "click", "timeout": 1}, {"cmd": "open"}]}
    assert protocol.command_deadline(run) == (2 + cap + slack) + (2 * t + cap + slack)


def test_env_limits(monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_IDLE_S", "3")
    monkeypatch.setenv("SYNAPSE_BROWSER_MAX_SESSIONS", "1")
    assert protocol.idle_s() == 3.0
    assert protocol.max_sessions() == 1
    monkeypatch.delenv("SYNAPSE_BROWSER_IDLE_S")
    assert protocol.idle_s() == 600.0
