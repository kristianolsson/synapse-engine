"""
Deny-rule settings passed to the Claude CLI (--settings) in auto permission mode.

A best-effort backstop, not a security boundary: the agent runs as the same OS
user as the engine, so a shell command can still reach these paths unless
Claude Code's auto-mode safety check blocks it. Real isolation (separate user /
secrets broker) is out of scope here.

Paths are absolute ("//" prefix = filesystem root in Claude Code permission
rules) and computed at runtime so the same rules work on macOS and in Docker.
~/.claude/projects stays readable: scheduled analysis reads session transcripts
and the agent re-reads its own saved tool results there.
"""

import json
from pathlib import Path

# Repo root: services/ingestion/providers/ -> repo
_ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_HOME_SECRET_PATHS = [
    ".ssh/**",
    ".claude/.credentials.json",
    ".claude/settings*.json",
    ".etrade_tokens",
    ".etrade_chrome_profile/**",
    ".synapse-browser/**",
    ".amazon-fresh-session/**",
]

_ENGINE_SECRET_FILES = [
    ".env",
    "token.json",
    "credentials.json",
    "smartthings_token.json",
]

_BASH_DENY = ["Bash(env)", "Bash(env *)", "Bash(printenv*)", "Bash(cat /proc/*)"]


def _abs(path: str) -> str:
    return "/" + path if path.startswith("/") else "//" + path


def build_agent_settings(home: str, engine_root: str) -> dict:
    home = home.rstrip("/")
    engine_root = engine_root.rstrip("/")
    secret_paths = ["/proc/**"]
    secret_paths += [f"{home}/{p}" for p in _HOME_SECRET_PATHS]
    secret_paths += [f"{engine_root}/{f}" for f in _ENGINE_SECRET_FILES]

    deny = []
    for p in secret_paths:
        deny.append(f"Read({_abs(p)})")
        deny.append(f"Edit({_abs(p)})")
    deny.append(f"Edit({_abs(engine_root)}/**)")
    deny.extend(_BASH_DENY)
    return {"permissions": {"deny": deny}}


def agent_settings_json() -> str:
    return json.dumps(
        build_agent_settings(str(Path.home()), str(_ENGINE_ROOT)),
        separators=(",", ":"),
    )
