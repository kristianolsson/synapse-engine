"""
Session manager for maintaining state in the Gemini CLI.

Stores a mapping of incoming sender IDs (email address or Telegram ID)
to a Gemini CLI session UUID, enforcing a Time-To-Live (TTL).
"""

import json
import logging
import os
import threading
import time
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages conversational state context for users."""

    def __init__(self, filepath: str = config.SESSION_STORAGE_PATH, ttl_minutes: int = config.SESSION_TTL_MINUTES):
        self.filepath = filepath
        self.ttl_seconds = ttl_minutes * 60
        self._lock = threading.Lock()
        self._stats_prefs: dict[str, bool] = {}  # per-user stats overrides (in-memory)

    def _provider_key(self, session_key: str) -> str:
        """Prefix a session key with the active provider to isolate session IDs across providers."""
        return f"{config.get_ai_provider()}:{session_key}"

    def _read_data(self) -> dict:
        """Read the JSON file and clean up expired sessions."""
        if not os.path.exists(self.filepath):
            return {}

        try:
            with open(self.filepath, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to read sessions file, starting fresh: %s", e)
            data = {}

        # Clean expired sessions on read
        now = time.time()
        
        from datetime import date
        now_date = date.fromtimestamp(now)
        
        cleaned_data = {}
        cleaned_count = 0
        for key, info in data.items():
            last_seen = info.get("last_seen", 0)
            
            # Check standard TTL
            if now - last_seen > self.ttl_seconds:
                cleaned_count += 1
                continue

            # Daily reset for main user sessions (e.g. Telegram's per-user
            # default session) — opt out via daily_reset=False for sessions
            # that should live purely by TTL instead (email threads,
            # Telegram per-message reply-branches). Defaults to True so
            # entries written before this flag existed keep today's behavior.
            if info.get("daily_reset", True):
                last_seen_date = date.fromtimestamp(last_seen) if last_seen else date.min
                if last_seen_date != now_date:
                    cleaned_count += 1
                    continue

            cleaned_data[key] = info

        if cleaned_count > 0:
            logger.debug("Cleaned up %d expired session(s).", cleaned_count)

        return cleaned_data

    def _write_data(self, data: dict) -> None:
        """Write the dictionary back to the JSON file safely."""
        try:
            with open(self.filepath, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error("Failed to write to sessions file: %s", e)

    def get_session(self, session_key: str) -> Optional[str]:
        """Get the active session ID for a user/thread, if it exists and hasn't expired."""
        pkey = self._provider_key(session_key)
        with self._lock:
            data = self._read_data()
            if pkey in data:
                return data[pkey].get("session_id")
            return None

    def save_session(self, session_key: str, session_id: str, daily_reset: bool = True) -> None:
        """Save a session ID and update its `last_seen` timestamp.

        `daily_reset` controls whether this entry is dropped at the next
        calendar-day boundary regardless of TTL (see `_read_data`). Defaults
        to True to match the historical behavior of a per-user main session
        resetting each morning; pass False for sessions that should live
        purely by TTL instead (email threads, Telegram per-message
        reply-branches).
        """
        if not session_id:
            return

        pkey = self._provider_key(session_key)
        with self._lock:
            data = self._read_data()
            data[pkey] = {
                "session_id": session_id,
                "last_seen": time.time(),
                "daily_reset": daily_reset,
            }
            self._write_data(data)
            logger.debug("Saved session_id %r for %r (provider=%s)", session_id, session_key, config.get_ai_provider())

    def clear_session(self, session_key: str) -> bool:
        """Explicitly delete a user's session. Returns True if one was deleted."""
        pkey = self._provider_key(session_key)
        with self._lock:
            data = self._read_data()
            if pkey in data:
                del data[pkey]
                self._write_data(data)
                logger.debug("Cleared session for %r", session_key)
                return True
            return False

    def get_message_session(self, message_id: int) -> Optional[str]:
        """Get the session ID associated with a specific Telegram message ID."""
        return self.get_session(f"msg_{message_id}")

    def save_message_session(self, message_id: int, session_id: str) -> None:
        """Save a session ID tied to a specific Telegram message ID."""
        self.save_session(f"msg_{message_id}", session_id, daily_reset=False)

    def get_stats_enabled(self, user_key: str) -> bool:
        """Return whether stats are enabled for a user, falling back to config default."""
        return self._stats_prefs.get(user_key, config.STATS_ENABLED)

    def set_stats_enabled(self, user_key: str, enabled: bool) -> None:
        """Set the per-user stats preference (in-memory only)."""
        self._stats_prefs[user_key] = enabled
        logger.debug("Stats preference for %r set to %s", user_key, enabled)

class UserSession:
    """A handle bound to one identity, wrapping a SessionManager (or, in
    tests, anything with its interface — a MagicMock configured with
    get_session/save_session/get_stats_enabled/etc. behaves identically,
    since every method here just calls straight through to `manager`).

    Not a substitute for SessionManager — pass it the caller's existing
    live instance, never a fresh SessionManager(), which would silently
    lose its in-memory state exactly like constructing SessionManager()
    directly does.

    `stats_key` only needs to be given when it differs from `key` — e.g.
    email threads use a per-thread `key` for session continuity but a
    per-sender `stats_key` for the /stats preference, since one person can
    have many threads. Defaults to `key` when omitted.

    `daily_reset` is passed straight through to `SessionManager.save_session`
    (see its docstring) — pass False for sessions that should live purely by
    TTL instead of also resetting at the next calendar-day boundary (email
    threads).
    """

    def __init__(
        self,
        manager: "SessionManager",
        key: str,
        stats_key: Optional[str] = None,
        daily_reset: bool = True,
    ):
        self._manager = manager
        self.key = key
        self.stats_key = stats_key if stats_key is not None else key
        self.daily_reset = daily_reset

    @property
    def session_id(self) -> Optional[str]:
        return self._manager.get_session(self.key)

    def save(self, session_id: str) -> None:
        self._manager.save_session(self.key, session_id, daily_reset=self.daily_reset)

    def clear(self) -> bool:
        return self._manager.clear_session(self.key)

    @property
    def stats_enabled(self) -> bool:
        return self._manager.get_stats_enabled(self.stats_key)

    def set_stats_enabled(self, enabled: bool) -> None:
        self._manager.set_stats_enabled(self.stats_key, enabled)
