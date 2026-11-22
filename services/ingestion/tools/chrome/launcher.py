"""Launch a plain Google Chrome and attach Playwright to it over CDP.

Why not Playwright's own launch: browsers Playwright launches report
navigator.webdriver === true (Firefox can't turn it off at all), and sites
with bot management reject them. A Chrome started here — no automation
flags — and attached via chromium.connect_over_cdp() reports false.

On Linux without a DISPLAY (the QNAP container) Chrome runs headed on an
Xvfb virtual display; headless Chrome announces itself as "HeadlessChrome".

The caller must hold an exclusive lock for the profile (etrade_cli holds
/tmp/etrade.lock): clear_stale_state() deletes Chrome's singleton locks.
"""
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
STALE_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort")


class ChromeLaunchError(RuntimeError):
    """Chrome (or its virtual display) could not be started or reached."""


def find_chrome() -> str:
    override = os.environ.get("SYNAPSE_CHROME_PATH")
    if override:
        return override
    for name in ("google-chrome", "google-chrome-stable", "chromium"):
        path = shutil.which(name)
        if path:
            return path
    if os.path.exists(MAC_CHROME):
        return MAC_CHROME
    raise ChromeLaunchError("Google Chrome not found (install it or set SYNAPSE_CHROME_PATH)")


def _free_port() -> int:
    # Port 0 makes Chrome report navigator.webdriver=true, so we pick a free port ourselves.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_args(chrome: str, profile_dir: Path, *, port: int, headless: bool = False,
               sandbox: bool = True) -> list:
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",  # Docker's default /dev/shm is 64 MB
        "--password-store=basic",  # fixed cookie key: profile stays readable across runs
        "--window-size=1280,800",
    ]
    if headless:
        args.append("--headless=new")
    if not sandbox:
        args.append("--no-sandbox")
    args.append("about:blank")
    return args


def clear_stale_state(profile_dir: Path) -> None:
    for name in STALE_FILES:
        path = Path(profile_dir) / name
        if os.path.lexists(path):
            path.unlink()


def wait_for_devtools(port: int, *, timeout_s: float = 20.0,
                      proc: Optional[subprocess.Popen] = None) -> None:
    # Fixed-port Chrome does not write DevToolsActivePort, so poll the endpoint instead.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise ChromeLaunchError(f"Chrome exited early (exit code {proc.returncode})")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    raise ChromeLaunchError("Chrome DevTools endpoint never came up")


def needs_virtual_display(headless: bool) -> bool:
    return not headless and platform.system() == "Linux" and not os.environ.get("DISPLAY")


def _start_xvfb() -> tuple:
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    os.close(write_fd)
    with os.fdopen(read_fd) as f:
        number = f.readline().strip()  # Xvfb writes the display number once ready
    if not number:
        _stop(proc, 2)
        raise ChromeLaunchError("Xvfb failed to start")
    return proc, f":{number}"


def _stop(proc: subprocess.Popen, grace_s: float) -> None:
    """SIGTERM the process group (lets Chrome flush the profile), then SIGKILL."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(grace_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(5)


@contextmanager
def chrome_cdp(playwright, profile_dir, *, headless: bool = False,
               sandbox: Optional[bool] = None) -> Iterator["object"]:
    """Start Chrome on `profile_dir`, yield its default BrowserContext, always clean up.

    sandbox=None reads SYNAPSE_CHROME_NO_SANDBOX ("1" disables Chrome's sandbox —
    needed only if the container can't provide one).
    """
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    if sandbox is None:
        sandbox = os.environ.get("SYNAPSE_CHROME_NO_SANDBOX", "") != "1"
    clear_stale_state(profile_dir)

    env = dict(os.environ)
    xvfb = None
    port = _free_port()
    try:
        if needs_virtual_display(headless):
            xvfb, env["DISPLAY"] = _start_xvfb()
        proc = subprocess.Popen(
            build_args(find_chrome(), profile_dir, port=port, headless=headless, sandbox=sandbox),
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # own process group, so _stop() reaches renderers too
        )
        try:
            wait_for_devtools(port, proc=proc)
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                yield browser.contexts[0]
            finally:
                browser.close()  # disconnects only; Chrome is stopped below
        finally:
            _stop(proc, 15)
    finally:
        if xvfb is not None:
            _stop(xvfb, 5)
