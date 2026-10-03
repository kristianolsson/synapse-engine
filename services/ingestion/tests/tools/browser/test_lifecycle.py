"""Lifecycle/cleanup tests: crashes, stale state, hangs, timers, caps, gc."""

import os
import signal
import socket
import time

import pytest

from services.ingestion.tools.browser import client, runtime
from services.ingestion.tools.browser.errors import BrowserError

pytestmark = pytest.mark.browser


@pytest.fixture
def rt(rt_root, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_WATCHDOG_S", "0.5")
    yield rt_root
    client.stop(all_sessions=True)


def _info(session="s1"):
    return runtime.read_info(runtime.session_paths(session))


def _wait(predicate, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _open_blank(session="s1"):
    return client.request(session, {"cmd": "open", "url": "about:blank"})


def test_kill9_daemon_then_next_command_recovers(rt):
    _open_blank()
    old = _info()
    os.kill(old["daemon_pid"], signal.SIGKILL)
    assert _wait(lambda: not runtime.is_locked(runtime.session_paths("s1").lock))
    _open_blank()
    assert _info()["daemon_pid"] != old["daemon_pid"]
    assert _wait(lambda: not runtime.group_alive(old["browser_pgid"], old["profile"]))


def test_kill9_firefox_then_next_command_recovers(rt):
    _open_blank()
    old = _info()
    runtime.kill_group(old["browser_pgid"])
    assert _wait(lambda: not runtime.is_locked(runtime.session_paths("s1").lock))
    _open_blank()
    assert _info()["daemon_pid"] != old["daemon_pid"]


def test_stale_socket_and_info(rt):
    paths = runtime.session_paths("s1")
    paths.dir.mkdir(parents=True)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(paths.sock))
    s.close()  # leaves a socket file with no listener
    runtime.write_info(paths, {"daemon_pid": 999999, "browser_pgid": 999999, "profile": str(paths.temp_profile)})
    assert _open_blank()["ok"]
    assert _info()["daemon_pid"] != 999999


def test_stale_firefox_profile_lock(rt):
    profile = runtime.profile_dir("p1")
    profile.mkdir(parents=True)
    for name in ("parent.lock", ".parentlock"):
        (profile / name).write_text("stale")
    assert "started" in client.start("s1", {"profile": "p1"})["message"]
    assert _open_blank()["ok"]
    for name in ("parent.lock", ".parentlock"):
        f = profile / name
        assert not f.exists() or f.read_text() != "stale"


def test_profile_in_use_by_other_session(rt):
    client.start("a", {"profile": "shared"})
    with pytest.raises(BrowserError) as e:
        client.start("b", {"profile": "shared"})
    assert e.value.code == "profile_locked"


def test_hung_page_times_out_daemon_stays_healthy(rt, site):
    client.start("s1", {"allow_private": True})
    with pytest.raises(BrowserError) as e:
        client.request("s1", {"cmd": "open", "url": site + "/hang", "timeout": 2})
    assert e.value.code == "timeout"
    assert client.request("s1", {"cmd": "open", "url": site + "/page2.html"})["header"]["title"] == "Page two"


def test_hung_daemon_is_killed(rt, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_CLIENT_SLACK_S", "1")
    _open_blank()
    info = _info()
    os.kill(info["daemon_pid"], signal.SIGSTOP)
    with pytest.raises(BrowserError) as e:
        client.request("s1", {"cmd": "snapshot", "timeout": 1})
    assert e.value.code == "daemon_hung"
    assert _wait(lambda: not runtime.is_locked(runtime.session_paths("s1").lock))
    assert _open_blank()["ok"]


def test_idle_timeout(rt, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_IDLE_S", "1")
    _open_blank()
    paths = runtime.session_paths("s1")
    assert _wait(lambda: not runtime.is_locked(paths.lock))
    assert not paths.sock.exists() and not paths.info.exists()
    assert "idle timeout" in paths.log.read_text()


def test_max_lifetime_despite_activity(rt, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_MAX_LIFETIME_S", "3")
    _open_blank()
    paths = runtime.session_paths("s1")
    lock = paths.lock
    successes = 0
    end = time.monotonic() + 15
    while runtime.is_locked(lock) and time.monotonic() < end:
        try:
            client.request("s1", {"cmd": "snapshot"}, autostart=False)
            successes += 1
        except BrowserError:
            pass
        time.sleep(0.5)
    assert not runtime.is_locked(lock)
    assert successes >= 1
    assert "max lifetime" in paths.log.read_text()


def test_memory_guard(rt, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_MAX_RSS_MB", "1")
    monkeypatch.setenv("SYNAPSE_BROWSER_RSS_CHECK_S", "0.5")
    _open_blank()
    assert _wait(lambda: not runtime.is_locked(runtime.session_paths("s1").lock))
    assert "memory guard" in runtime.session_paths("s1").log.read_text()


def test_sigterm_shuts_down_cleanly(rt):
    _open_blank()
    info = _info()
    paths = runtime.session_paths("s1")
    assert paths.temp_profile.exists()
    os.kill(info["daemon_pid"], signal.SIGTERM)
    assert _wait(lambda: not runtime.is_locked(paths.lock))
    assert _wait(lambda: not runtime.group_alive(info["browser_pgid"], info["profile"]))
    assert not paths.temp_profile.exists()
    assert not paths.sock.exists() and not paths.info.exists()
    assert "signal SIGTERM" in paths.log.read_text()


def test_session_cap(rt, monkeypatch):
    monkeypatch.setenv("SYNAPSE_BROWSER_MAX_SESSIONS", "1")
    _open_blank("alpha")
    with pytest.raises(BrowserError) as e:
        _open_blank("beta")
    assert e.value.code == "session_cap" and "alpha" in e.value.message
    beta = runtime.session_paths("beta")
    assert _wait(lambda: not runtime.is_locked(beta.lock), 5)  # daemon exits right after reporting the error
    assert not beta.info.exists()


def test_status_and_gc_for_stale_session(rt):
    paths = runtime.session_paths("dead")
    paths.dir.mkdir(parents=True)
    runtime.write_info(paths, {"daemon_pid": 999999})
    assert "dead: stale" in client.status()["message"]
    assert "dead" in client.gc()["message"]
    assert not paths.info.exists() and paths.lock.exists()
