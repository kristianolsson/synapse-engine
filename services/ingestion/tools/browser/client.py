"""Thin client for the browser daemon: request, auto-start, start/stop/status/gc.

Must not import Playwright — it runs on every CLI invocation.
"""

import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import protocol, runtime
from .errors import BrowserError

REPO_ROOT = Path(__file__).resolve().parents[4]
DAEMON_MODULE = "services.ingestion.tools.browser.daemon"
PING_TIMEOUT_S = 5.0
STOP_WAIT_S = 20.0


def normalize_opts(opts: dict) -> dict:
    return {
        "profile": opts.get("profile") or None,
        "headed": bool(opts.get("headed")),
        "allow_private": bool(opts.get("allow_private")),
    }


def _send(paths: runtime.SessionPaths, req: dict, timeout: float) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(paths.sock))
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection without a response")
            buf += chunk
    return json.loads(buf)


def _ping(paths: runtime.SessionPaths) -> bool:
    try:
        return bool(_send(paths, {"cmd": "ping"}, PING_TIMEOUT_S).get("pong"))
    except (OSError, ValueError):
        return False


def _wait_ready(paths: runtime.SessionPaths, timeout: float) -> bool:
    """Wait for a daemon that another client is starting. False if it died or never answered."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _ping(paths):
            return True
        if not runtime.is_locked(paths.lock):
            return False
        time.sleep(0.1)
    return False


def _kill_unresponsive(session: str, paths: runtime.SessionPaths) -> BrowserError:
    info = runtime.read_info(paths) or {}
    pid = info.get("daemon_pid")
    if pid and runtime.process_matches(pid, [DAEMON_MODULE, f"--session {session}"]):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pgid, profile = info.get("browser_pgid"), info.get("profile")
    if pgid and profile and runtime.group_alive(pgid, profile):
        runtime.kill_group(pgid)
    return BrowserError(
        f"daemon for session {session} was unresponsive and has been killed — re-run the command",
        "daemon_hung",
    )


def request(session: str, req: dict, start_opts: Optional[dict] = None, autostart: bool = True) -> dict:
    paths = runtime.session_paths(session)
    deadline = protocol.command_deadline(req) + protocol.client_slack_s()
    try:
        resp, restart = None, False
        try:
            resp = _send(paths, req, deadline)
            if (resp.get("details") or {}).get("shutting_down") and autostart:
                _wait_unlocked(paths.lock, STOP_WAIT_S)  # command was not run; safe to replay on a new daemon
                restart = True
        except (FileNotFoundError, ConnectionRefusedError):
            if not autostart:
                raise BrowserError(f"session {session} is not running", "no_session")
            restart = True
        if restart:
            msg = spawn_daemon(session, start_opts or {})
            if msg.get("already_running") and not _wait_ready(paths, protocol.start_timeout_s()):
                if runtime.is_locked(paths.lock):
                    raise _kill_unresponsive(session, paths)  # holds the lock but never listens: wedged
                # The previous daemon finished shutting down meanwhile: start a fresh one.
                if spawn_daemon(session, start_opts or {}).get("already_running") and not _wait_ready(
                    paths, protocol.start_timeout_s()
                ):
                    raise BrowserError(f"daemon for session {session} exited during startup — see {paths.log}",
                                       "daemon_start_failed")
            resp = _send(paths, req, deadline)
    except TimeoutError:  # before OSError: TimeoutError is an OSError subclass
        if _ping(paths):
            raise BrowserError(f"no response within {deadline:g}s (daemon is alive)", "timeout")
        raise _kill_unresponsive(session, paths)
    except (OSError, ValueError) as e:
        raise BrowserError(f"lost connection to daemon: {e}", "daemon_error")
    if not resp.get("ok"):
        raise BrowserError(resp.get("error", "unknown error"), resp.get("code", "daemon_error"), resp.get("details"))
    return resp


def _read_ready(fd: int, timeout: float) -> Optional[dict]:
    end = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            return None
        chunk = os.read(fd, 4096)
        if not chunk:
            return None  # daemon exited without reporting
        buf += chunk
    return json.loads(buf.split(b"\n", 1)[0])


def spawn_daemon(session: str, opts: dict) -> dict:
    paths = runtime.session_paths(session)
    paths.dir.mkdir(parents=True, exist_ok=True)
    opts = normalize_opts(opts)
    read_fd, write_fd = os.pipe()
    cmd = [sys.executable, "-m", DAEMON_MODULE, "--session", session, "--ready-fd", str(write_fd)]
    if opts["profile"]:
        cmd += ["--profile", opts["profile"]]
    if opts["headed"]:
        cmd.append("--headed")
    if opts["allow_private"]:
        cmd.append("--allow-private")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO_ROOT), env.get("PYTHONPATH")) if p)
    try:
        with open(paths.log, "ab") as log:
            subprocess.Popen(
                cmd, cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                pass_fds=(write_fd,), start_new_session=True,
            )
    finally:
        os.close(write_fd)
    try:
        msg = _read_ready(read_fd, protocol.start_timeout_s())
    finally:
        os.close(read_fd)
    if msg is None:
        raise BrowserError(
            f"browser daemon did not start within {protocol.start_timeout_s():g}s — see {paths.log}",
            "daemon_start_failed",
        )
    if not msg.get("ok"):
        raise BrowserError(msg.get("error", "daemon failed to start"), msg.get("code", "daemon_start_failed"))
    return msg


def start(session: str, opts: dict) -> dict:
    paths = runtime.session_paths(session)
    wanted = normalize_opts(opts)
    if not runtime.is_locked(paths.lock):
        if not spawn_daemon(session, wanted).get("already_running"):
            return {"ok": True, "message": f"session {session} started"}
    # Running (or being started by another client): wait until it answers, then compare options.
    if not _wait_ready(paths, protocol.start_timeout_s()):
        raise BrowserError(f"session {session} is not responding — see {paths.log}", "daemon_error")
    running = (runtime.read_info(paths) or {}).get("options")
    if running != wanted:
        raise BrowserError(
            f"session {session} is already running with different options ({running}) — stop it first",
            "session_conflict",
        )
    return {"ok": True, "message": f"session {session} already running"}


def _wait_unlocked(lock: Path, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not runtime.is_locked(lock):
            return True
        time.sleep(0.1)
    return False


def stop(session: str = "default", all_sessions: bool = False) -> dict:
    names = runtime.live_sessions() if all_sessions else [session]
    stopped = []
    for name in names:
        try:
            request(name, {"cmd": "stop"}, autostart=False)
        except BrowserError as e:
            if e.code != "no_session":
                raise
            if not runtime.is_locked(runtime.session_paths(name).lock):
                continue  # not running; otherwise it is starting or stopping: wait for it below
        stopped.append(name)
    for name in stopped:
        lock = runtime.session_paths(name).lock
        if _wait_unlocked(lock, STOP_WAIT_S):
            continue
        pid = (runtime.read_info(runtime.session_paths(name)) or {}).get("daemon_pid")
        if pid and runtime.process_matches(pid, [DAEMON_MODULE, f"--session {name}"]):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if _wait_unlocked(lock, 5.0):
                continue
        raise BrowserError(f"session {name} did not exit within {STOP_WAIT_S:g}s", "daemon_error")
    return {"ok": True, "message": "stopped: " + (", ".join(stopped) or "(none running)")}


def status() -> dict:
    lines = []
    untrusted = []  # page-derived (URLs): rendered inside the untrusted fence
    for name in runtime.all_sessions():
        paths = runtime.session_paths(name)
        if not runtime.is_locked(paths.lock):
            if paths.info.exists():
                lines.append(f"{name}: stale (run 'browser gc')")
            continue
        try:
            st = request(name, {"cmd": "status", "timeout": 1}, autostart=False)["status"]
        except BrowserError as e:
            lines.append(f"{name}: unresponsive ({e.message})")
            continue
        lines.append(
            f"{name}: age {st['age_s']}s, idle {st['idle_s']}s, rss {st['rss_mb']} MB, "
            f"profile {st['profile'] or '(temporary)'}"
        )
        untrusted.append(f"{name}: url {st['url']}")
    resp = {"ok": True, "message": "\n".join(lines) or "no sessions"}
    if untrusted:
        resp["untrusted"] = "\n".join(untrusted)
    return resp


def gc() -> dict:
    cleaned = []
    for name in runtime.all_sessions():
        paths = runtime.session_paths(name)
        fd = runtime.try_lock(paths.lock)
        if fd is None:
            continue
        try:
            runtime.sweep(paths)
            for entry in paths.dir.iterdir():  # keep daemon.lock: removing it would let two daemons lock different inodes
                if entry == paths.lock:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            cleaned.append(name)
        finally:
            os.close(fd)
    return {"ok": True, "message": "cleaned: " + (", ".join(cleaned) or "(nothing stale)")}
