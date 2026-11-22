import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from services.ingestion.tools.chrome import launcher
from services.ingestion.tools.chrome.launcher import (
    ChromeLaunchError, build_args, clear_stale_state, find_chrome,
    _free_port, needs_virtual_display, wait_for_devtools,
)


def test_find_chrome_prefers_env_override(monkeypatch):
    monkeypatch.setenv("SYNAPSE_CHROME_PATH", "/opt/custom/chrome")
    assert find_chrome() == "/opt/custom/chrome"


def test_find_chrome_uses_path_lookup(monkeypatch):
    monkeypatch.delenv("SYNAPSE_CHROME_PATH", raising=False)
    monkeypatch.setattr(launcher.shutil, "which",
                        lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None)
    assert find_chrome() == "/usr/bin/google-chrome"


def test_find_chrome_raises_when_missing(monkeypatch):
    monkeypatch.delenv("SYNAPSE_CHROME_PATH", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    monkeypatch.setattr(launcher, "MAC_CHROME", "/nonexistent/chrome")
    with pytest.raises(ChromeLaunchError):
        find_chrome()


def test_free_port_returns_valid_port():
    port = _free_port()
    assert isinstance(port, int)
    assert 1 <= port <= 65535


def test_build_args_has_no_automation_flags_and_required_switches(tmp_path):
    args = build_args("/usr/bin/google-chrome", tmp_path, port=9555)
    assert args[0] == "/usr/bin/google-chrome"
    assert "--remote-debugging-port=9555" in args
    assert "--remote-debugging-port=0" not in args
    assert f"--user-data-dir={tmp_path}" in args
    assert "--password-store=basic" in args
    assert "--enable-automation" not in args
    assert not any(a.startswith("--headless") for a in args)
    assert "--no-sandbox" not in args


def test_build_args_headless_and_no_sandbox(tmp_path):
    args = build_args("chrome", tmp_path, port=9555, headless=True, sandbox=False)
    assert "--headless=new" in args
    assert "--no-sandbox" in args


def test_clear_stale_state_removes_singletons_and_port_file(tmp_path):
    (tmp_path / "SingletonLock").symlink_to("host-12345")  # dangling, like a crashed Chrome
    (tmp_path / "SingletonSocket").write_text("")
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/x")
    (tmp_path / "Default").mkdir()
    clear_stale_state(tmp_path)
    assert not os.path.lexists(tmp_path / "SingletonLock")
    assert not (tmp_path / "SingletonSocket").exists()
    assert not (tmp_path / "DevToolsActivePort").exists()
    assert (tmp_path / "Default").is_dir()


def test_wait_for_devtools_returns_when_endpoint_answers():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wait_for_devtools(server.server_address[1], timeout_s=5)
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_devtools_times_out():
    with pytest.raises(ChromeLaunchError, match="never came up"):
        wait_for_devtools(_free_port(), timeout_s=0.3)


def test_wait_for_devtools_reports_early_exit():
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    proc.wait()
    with pytest.raises(ChromeLaunchError, match="exited early"):
        wait_for_devtools(_free_port(), timeout_s=2, proc=proc)


def test_needs_virtual_display(monkeypatch):
    monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert needs_virtual_display(headless=False) is True
    assert needs_virtual_display(headless=True) is False
    monkeypatch.setenv("DISPLAY", ":0")
    assert needs_virtual_display(headless=False) is False
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert needs_virtual_display(headless=False) is False


def _chrome_available():
    try:
        find_chrome()
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _chrome_available(), reason="Google Chrome / Playwright not installed")
def test_chrome_cdp_real_chrome_reports_no_webdriver(tmp_path):
    from playwright.sync_api import sync_playwright
    from services.ingestion.tools.chrome import chrome_cdp

    with sync_playwright() as p:
        with chrome_cdp(p, tmp_path / "profile", headless=True) as context:
            page = context.new_page()
            page.goto("about:blank")
            assert page.evaluate("navigator.webdriver") is False
    # Chrome is gone and the profile was flushed to disk
    assert (tmp_path / "profile" / "Default").is_dir()


def test_profile_lock_free_then_released(tmp_path):
    profile = tmp_path / "profile"
    with launcher._profile_lock(profile, 0.5):
        pass
    with launcher._profile_lock(profile, 0.5):  # re-acquirable after release
        pass


def test_profile_lock_held_by_another_times_out(tmp_path):
    import fcntl
    profile = tmp_path / "profile"
    fd = os.open(tmp_path / "profile.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(ChromeLaunchError, match="in use"):
            with launcher._profile_lock(profile, 0.5):
                pass
    finally:
        os.close(fd)


def test_clear_stale_state_does_not_kill_unrelated_live_process(tmp_path):
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        os.symlink(f"{socket.gethostname()}-{proc.pid}", tmp_path / "SingletonLock")
        clear_stale_state(tmp_path)  # cmdline lacks the profile path (or no /proc): no kill
        assert proc.poll() is None
        assert not os.path.lexists(tmp_path / "SingletonLock")
    finally:
        proc.kill()
        proc.wait()


def test_clear_stale_state_unlinks_lock_for_dead_pid_or_other_host(tmp_path):
    dead = subprocess.Popen(["true"])
    dead.wait()
    os.symlink(f"{socket.gethostname()}-{dead.pid}", tmp_path / "SingletonLock")
    clear_stale_state(tmp_path)
    assert not os.path.lexists(tmp_path / "SingletonLock")
    os.symlink("some-other-host-1", tmp_path / "SingletonLock")
    clear_stale_state(tmp_path)
    assert not os.path.lexists(tmp_path / "SingletonLock")


def test_start_xvfb_missing_binary_raises_and_closes_fds(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("Xvfb")
    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    before = len(os.listdir("/dev/fd"))
    with pytest.raises(ChromeLaunchError, match="Xvfb not found"):
        launcher._start_xvfb()
    assert len(os.listdir("/dev/fd")) == before
