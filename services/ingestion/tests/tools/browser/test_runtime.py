"""Unit tests for runtime paths, locking, process helpers, and the stale-state sweep."""

import os
import subprocess

import pytest

from services.ingestion.tools.browser import runtime
from services.ingestion.tools.browser.errors import BrowserError


def test_session_paths_layout(rt_root):
    p = runtime.session_paths("fares")
    assert p.dir == rt_root / "rt" / "fares"
    assert p.sock.name == "daemon.sock" and p.lock.name == "daemon.lock" and p.info.name == "daemon.json"
    assert p.artifacts == p.dir / "artifacts" and p.temp_profile == p.dir / "profile"


@pytest.mark.parametrize("bad", ["../x", "", "a b", "x" * 33])
def test_bad_names_rejected(rt_root, bad):
    with pytest.raises(BrowserError) as e:
        runtime.session_paths(bad)
    assert e.value.code == "usage_error"
    with pytest.raises(BrowserError):
        runtime.profile_dir(bad)


def test_profile_dir(rt_root):
    assert runtime.profile_dir("sas") == rt_root / "profiles" / "sas"


def test_try_lock_is_exclusive(tmp_path):
    lock = tmp_path / "daemon.lock"
    fd = runtime.try_lock(lock)
    assert fd is not None
    assert runtime.try_lock(lock) is None
    assert runtime.is_locked(lock)
    os.close(fd)
    assert not runtime.is_locked(lock)
    assert not runtime.is_locked(tmp_path / "missing.lock")


def test_info_roundtrip_and_corrupt(rt_root):
    p = runtime.session_paths("s1")
    p.dir.mkdir(parents=True)
    runtime.write_info(p, {"daemon_pid": 1})
    assert runtime.read_info(p) == {"daemon_pid": 1}
    p.info.write_text("{nope")
    assert runtime.read_info(p) is None


def test_parse_ps():
    text = "  101  101  2048 /usr/bin/firefox -profile /tmp/x/profile\n  102 101 1024 plugin-container\nbad line\n"
    procs = runtime.parse_ps(text)
    assert procs[0] == runtime.Proc(pid=101, pgid=101, rss_kb=2048, command="/usr/bin/firefox -profile /tmp/x/profile")
    assert procs[1].command == "plugin-container"
    assert len(procs) == 2


def _orphan(marker: str) -> subprocess.Popen:
    # A process-group leader whose command line contains `marker`, like Firefox with -profile <path>.
    return subprocess.Popen(["/bin/sh", "-c", f"sleep 60; : {marker}"], start_new_session=True)


def test_group_helpers_and_kill():
    proc = _orphan("/tmp/sbt-marker-profile")
    try:
        assert runtime.group_alive(proc.pid, "/tmp/sbt-marker-profile")
        assert not runtime.group_alive(proc.pid, "/somewhere/else")
        assert runtime.group_rss_mb(proc.pid) > 0
        assert runtime.process_matches(proc.pid, ["sleep 60", "sbt-marker-profile"])
        runtime.kill_group(proc.pid)
        proc.wait(timeout=5)
        assert not runtime.group_alive(proc.pid, "/tmp/sbt-marker-profile")
    finally:
        if proc.poll() is None:
            proc.kill()
    runtime.kill_group(proc.pid)  # already gone: must not raise


def test_sweep_kills_orphan_and_removes_stale_files(rt_root):
    p = runtime.session_paths("s1")
    p.artifacts.mkdir(parents=True)
    p.temp_profile.mkdir(parents=True)
    profile = rt_root / "profiles" / "sas"
    profile.mkdir(parents=True)
    for name in runtime.FIREFOX_LOCK_FILES:
        (profile / name).write_text("")
    p.sock.write_text("")
    proc = _orphan(str(profile))
    runtime.write_info(p, {"browser_pgid": proc.pid, "profile": str(profile)})

    actions = runtime.sweep(p)

    proc.wait(timeout=5)
    assert any("killed" in a for a in actions)
    assert not p.sock.exists() and not p.info.exists()
    assert not p.artifacts.exists() and not p.temp_profile.exists()
    assert not any((profile / n).exists() for n in runtime.FIREFOX_LOCK_FILES)


def test_sweep_does_not_kill_unrelated_group(rt_root):
    p = runtime.session_paths("s1")
    p.dir.mkdir(parents=True)
    proc = _orphan("/tmp/unrelated")
    try:
        runtime.write_info(p, {"browser_pgid": proc.pid, "profile": "/tmp/sbt-other-profile"})
        runtime.sweep(p)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_live_and_all_sessions(rt_root):
    a, b = runtime.session_paths("a"), runtime.session_paths("b")
    a.dir.mkdir(parents=True)
    b.dir.mkdir(parents=True)
    fd = runtime.try_lock(a.lock)
    os.close(runtime.try_lock(b.lock))  # b's lock file exists but nobody holds it
    try:
        assert runtime.all_sessions() == ["a", "b"]
        assert runtime.live_sessions() == ["a"]
    finally:
        os.close(fd)
