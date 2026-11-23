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
from typing import Iterable, Optional

from .. import config

# Repo root: services/ingestion/providers/ -> repo
_ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_HOME_SECRET_PATHS = [
    ".ssh/**",
    ".claude/.credentials.json",
    ".claude.json",
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


def build_agent_settings(
    home: str,
    engine_root: str,
    extra_secret_files: Iterable[str] = (),
    vault_root: Optional[str] = None,
) -> dict:
    home = home.rstrip("/")
    engine_root = engine_root.rstrip("/")
    secret_paths = ["/proc/**"]
    secret_paths += [f"{home}/{p}" for p in _HOME_SECRET_PATHS]
    engine_files = list(_ENGINE_SECRET_FILES) + [str(f) for f in extra_secret_files]
    for f in engine_files:
        # Relative paths resolve against the engine root, like the config defaults.
        secret_paths.append(f if f.startswith("/") else f"{engine_root}/{f}")

    deny = []
    for p in dict.fromkeys(secret_paths):
        deny.append(f"Read({_abs(p)})")
        deny.append(f"Edit({_abs(p)})")
    deny.append(f"Edit({_abs(engine_root)}/**)")
    if vault_root:
        # The agent must not rewrite its own Claude config (hooks, permissions).
        deny.append(f"Edit({_abs(vault_root.rstrip('/'))}/.claude/settings*.json)")
    deny.extend(_BASH_DENY)
    return {"permissions": {"deny": deny}}


def agent_settings_json() -> str:
    return json.dumps(
        build_agent_settings(
            str(Path.home()),
            str(_ENGINE_ROOT),
            extra_secret_files=[
                config.CALENDAR_TOKEN_PATH,
                config.CALENDAR_CREDENTIALS_PATH,
                config.SMARTTHINGS_TOKEN_PATH,
            ],
            vault_root=config.VAULT_PATH,
        ),
        separators=(",", ":"),
    )
