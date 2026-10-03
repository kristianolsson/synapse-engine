"""Runtime layout, single-instance locking, process helpers, and stale-state sweep.

Liveness is decided by flock on daemon.lock, never by a PID file alone: the
kernel releases the lock when the daemon dies (even on kill -9).
"""

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import BrowserError
from .protocol import NAME_RE

PROFILE_LOCK = ".synapse.lock"
FIREFOX_LOCK_FILES = ("parent.lock", ".parentlock", "lock")


def runtime_root() -> Path:
    return Path(os.environ.get("SYNAPSE_BROWSER_RUNTIME_DIR") or "/tmp/synapse-browser")


def profiles_root() -> Path:
    env = os.environ.get("SYNAPSE_BROWSER_PROFILE_DIR")
    return Path(env) if env else Path.home() / ".synapse-browser" / "profiles"


def _check_name(kind: str, name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise BrowserError(f"invalid {kind} name {name!r}: use 1-32 letters, digits, '_' or '-'", "usage_error")


@dataclass(frozen=True)
class SessionPaths:
    dir: Path

    @property
    def lock(self) -> Path:
        return self.dir / "daemon.lock"

    @property
    def sock(self) -> Path:
        return self.dir / "daemon.sock"

    @property
    def info(self) -> Path:
        return self.dir / "daemon.json"

    @property
    def log(self) -> Path:
        return self.dir / "daemon.log"

    @property
    def artifacts(self) -> Path:
        return self.dir / "artifacts"

    @property
    def temp_profile(self) -> Path:
        return self.dir / "profile"


def session_paths(name: str) -> SessionPaths:
    _check_name("session", name)
    return SessionPaths(runtime_root() / name)


def profile_dir(name: str) -> Path:
    _check_name("profile", name)
    return profiles_root() / name


def try_lock(path: Path, wait_s: float = 0.0) -> Optional[int]:
    """Take an exclusive non-blocking flock. Returns the fd (keep it open to hold the lock) or None.

    If wait_s > 0, retries every ~20ms until wait_s elapses.
    """
    deadline = time.time() + wait_s
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            os.close(fd)
            if wait_s <= 0 or time.time() >= deadline:
                return None
            time.sleep(0.020)


def is_locked(path: Path) -> bool:
    if not path.exists():
        return False
    fd = try_lock(path)
    if fd is None:
        return True
    os.close(fd)
    return False


def write_info(paths: SessionPaths, info: dict) -> None:
    tmp = paths.info.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(info, indent=2))
    tmp.replace(paths.info)


def read_info(paths: SessionPaths) -> Optional[dict]:
    try:
        return json.loads(paths.info.read_text())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Proc:
    pid: int
    pgid: int
    rss_kb: int
    command: str


def parse_ps(text: str) -> list:
    procs = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            procs.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        except ValueError:
            continue
    return procs


def list_processes() -> list:
    out = subprocess.run(["ps", "-axo", "pid=,pgid=,rss=,command="], capture_output=True, text=True, check=False)
    return parse_ps(out.stdout)


def _has_token(command: str, needle: str) -> bool:
    """Check if needle appears in command followed by whitespace or end of line (not substring)."""
    return re.search(re.escape(needle) + r"(?=\s|$)", command) is not None


def group_alive(pgid: int, must_contain: str) -> bool:
    return any(p.pgid == pgid and _has_token(p.command, must_contain) for p in list_processes())


def kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def group_rss_mb(pgid: int) -> float:
    return sum(p.rss_kb for p in list_processes() if p.pgid == pgid) / 1024


def process_matches(pid: int, needles: list) -> bool:
    for p in list_processes():
        if p.pid == pid:
            return all(_has_token(p.command, n) for n in needles)
    return False


def find_browser(profile: Path) -> Optional[Proc]:
    """The Firefox process-group leader launched with `-profile <profile>`."""
    marker = f"-profile {profile}"
    candidates = [p for p in list_processes() if _has_token(p.command, marker)]
    leaders = [p for p in candidates if p.pid == p.pgid]
    return (leaders or candidates or [None])[0]


def clear_profile_locks(profile: Path) -> None:
    for name in FIREFOX_LOCK_FILES:
        try:
            (profile / name).unlink()
        except FileNotFoundError:
            pass


def cleanup(paths: SessionPaths) -> None:
    """Remove per-run state. The lock file and log stay (the log is useful after a crash)."""
    for f in (paths.sock, paths.info):
        f.unlink(missing_ok=True)
    shutil.rmtree(paths.artifacts, ignore_errors=True)
    shutil.rmtree(paths.temp_profile, ignore_errors=True)


def sweep(paths: SessionPaths) -> list:
    """Clean up after a dead daemon. Caller must hold paths.lock."""
    actions = []
    info = read_info(paths) or {}
    pgid, profile = info.get("browser_pgid"), info.get("profile")
    if pgid and profile and group_alive(pgid, profile):
        kill_group(pgid)
        actions.append(f"killed orphan browser group {pgid}")
    if profile:
        clear_profile_locks(Path(profile))
    cleanup(paths)
    return actions


def all_sessions() -> list:
    root = runtime_root()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and NAME_RE.match(d.name))


def live_sessions() -> list:
    return [n for n in all_sessions() if is_locked(runtime_root() / n / "daemon.lock")]
