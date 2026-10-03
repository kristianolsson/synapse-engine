"""Constants and env-driven limits shared by the browser client and daemon.

Must not import Playwright: the client imports this module on every call.
"""

import os
import re

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

DEFAULT_TIMEOUT_S = 10.0
WAIT_DEFAULT_TIMEOUT_S = 20.0
SETTLE_CAP_S = 5.0
QUIET_S = 0.5
LONG_REQUEST_S = 3.0
DEFAULT_TEXT_MAX = 8000
DAEMON_SLACK_S = 5.0


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "")
    return float(value) if value else default


def idle_s() -> float:
    return _env_float("SYNAPSE_BROWSER_IDLE_S", 600.0)


def max_lifetime_s() -> float:
    return _env_float("SYNAPSE_BROWSER_MAX_LIFETIME_S", 3600.0)


def max_rss_mb() -> float:
    return _env_float("SYNAPSE_BROWSER_MAX_RSS_MB", 1536.0)


def rss_check_s() -> float:
    return _env_float("SYNAPSE_BROWSER_RSS_CHECK_S", 15.0)


def max_sessions() -> int:
    return int(_env_float("SYNAPSE_BROWSER_MAX_SESSIONS", 2))


def watchdog_s() -> float:
    return _env_float("SYNAPSE_BROWSER_WATCHDOG_S", 5.0)


def start_timeout_s() -> float:
    return _env_float("SYNAPSE_BROWSER_START_TIMEOUT_S", 15.0)


def client_slack_s() -> float:
    return _env_float("SYNAPSE_BROWSER_CLIENT_SLACK_S", 5.0)


def command_deadline(req: dict) -> float:
    """Daemon-side deadline: action timeout + --wait-for timeout + settle cap + slack."""
    if req.get("cmd") == "run":
        steps = [s for s in req.get("steps") or [] if isinstance(s, dict)]
        return sum(command_deadline(s) for s in steps) or DAEMON_SLACK_S
    timeout = float(req.get("timeout", DEFAULT_TIMEOUT_S))
    return 2 * timeout + SETTLE_CAP_S + DAEMON_SLACK_S
