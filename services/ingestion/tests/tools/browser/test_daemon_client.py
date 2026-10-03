"""Daemon + client integration: auto-start, reuse, stop, start options."""

import concurrent.futures

import pytest

from services.ingestion.tools.browser import client, runtime
from services.ingestion.tools.browser.daemon import DAEMON_MODULE
from services.ingestion.tools.browser.errors import BrowserError

pytestmark = pytest.mark.browser


@pytest.fixture
def rt(rt_root, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_WATCHDOG_S", "0.5")
    yield rt_root
    client.stop(all_sessions=True)


def _daemon_count(session):
    return sum(1 for p in runtime.list_processes() if DAEMON_MODULE in p.command and f"--session {session}" in p.command)


def test_autostart_and_reuse(rt):
    resp = client.request("s1", {"cmd": "open", "url": "about:blank"})
    assert resp["header"]["url"] == "about:blank"
    pid = runtime.read_info(runtime.session_paths("s1"))["daemon_pid"]
    client.request("s1", {"cmd": "snapshot"})
    assert runtime.read_info(runtime.session_paths("s1"))["daemon_pid"] == pid
    assert runtime.live_sessions() == ["s1"]


def test_stop_cleans_up(rt):
    client.request("s1", {"cmd": "open", "url": "about:blank"})
    info = runtime.read_info(runtime.session_paths("s1"))
    assert "s1" in client.stop("s1")["message"]
    paths = runtime.session_paths("s1")
    assert not runtime.is_locked(paths.lock)
    assert not paths.sock.exists() and not paths.info.exists() and not paths.temp_profile.exists()
    assert not runtime.group_alive(info["browser_pgid"], info["profile"])


def test_request_without_autostart(rt):
    with pytest.raises(BrowserError) as e:
        client.request("s1", {"cmd": "snapshot"}, autostart=False)
    assert e.value.code == "no_session"


def test_concurrent_autostart_converges(rt):
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: client.request("race", {"cmd": "snapshot"}), range(4)))
    assert all(r["ok"] for r in results)
    assert _daemon_count("race") == 1


def test_start_options_and_conflict(rt, site):
    assert "started" in client.start("s1", {"allow_private": True})["message"]
    assert "already running" in client.start("s1", {"allow_private": True})["message"]
    with pytest.raises(BrowserError) as e:
        client.start("s1", {})
    assert e.value.code == "session_conflict"
    resp = client.request("s1", {"cmd": "open", "url": site + "/page2.html"})
    assert resp["header"]["title"] == "Page two"


def test_private_blocked_by_default(rt, site):
    with pytest.raises(BrowserError) as e:
        client.request("s1", {"cmd": "open", "url": site + "/page2.html"})
    assert e.value.code == "policy_blocked"


def test_status_lists_live_session(rt):
    client.request("s1", {"cmd": "open", "url": "about:blank"})
    msg = client.status()["message"]
    assert msg.startswith("s1: ") and "about:blank" in msg


def test_request_during_shutdown_respawns(rt):
    client.request("s1", {"cmd": "open", "url": "about:blank"})
    old = runtime.read_info(runtime.session_paths("s1"))["daemon_pid"]
    client.request("s1", {"cmd": "stop"}, autostart=False)
    resp = client.request("s1", {"cmd": "open", "url": "about:blank"})
    assert resp["ok"]
    assert runtime.read_info(runtime.session_paths("s1"))["daemon_pid"] != old


def test_stop_waits_for_session_already_stopping(rt):
    client.request("s1", {"cmd": "open", "url": "about:blank"})
    client.request("s1", {"cmd": "stop"}, autostart=False)
    client.stop("s1")
    assert not runtime.is_locked(runtime.session_paths("s1").lock)


def test_gc_keeps_lock_file(rt):
    paths = runtime.session_paths("old")
    paths.dir.mkdir(parents=True)
    paths.info.write_text("{}")
    paths.artifacts.mkdir()
    msg = client.gc()["message"]
    assert "old" in msg
    assert not paths.info.exists() and not paths.artifacts.exists()
    assert paths.lock.exists()
