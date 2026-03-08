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
        cleaned_data = {}
        cleaned_count = 0
        for key, info in data.items():
            last_seen = info.get("last_seen", 0)
            if now - last_seen <= self.ttl_seconds:
                cleaned_data[key] = info
            else:
                cleaned_count += 1

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
        with self._lock:
            data = self._read_data()
            if session_key in data:
                return data[session_key].get("session_id")
            return None

    def save_session(self, session_key: str, session_id: str) -> None:
        """Save a session ID and update its `last_seen` timestamp."""
        if not session_id:
            return

        with self._lock:
            data = self._read_data()
            data[session_key] = {
                "session_id": session_id,
                "last_seen": time.time()
            }
            self._write_data(data)
            logger.debug("Saved session_id %r for %r", session_id, session_key)

    def clear_session(self, session_key: str) -> bool:
        """Explicitly delete a user's session. Returns True if one was deleted."""
        with self._lock:
            data = self._read_data()
            if session_key in data:
                del data[session_key]
                self._write_data(data)
                logger.debug("Cleared session for %r", session_key)
                return True
            return False

    def get_message_session(self, message_id: int) -> Optional[str]:
        """Get the session ID associated with a specific Telegram message ID."""
        return self.get_session(f"msg_{message_id}")

    def save_message_session(self, message_id: int, session_id: str) -> None:
        """Save a session ID tied to a specific Telegram message ID."""
        self.save_session(f"msg_{message_id}", session_id)

    def get_stats_enabled(self, user_key: str) -> bool:
        """Return whether stats are enabled for a user, falling back to config default."""
        return self._stats_prefs.get(user_key, config.STATS_ENABLED)

    def set_stats_enabled(self, user_key: str, enabled: bool) -> None:
        """Set the per-user stats preference (in-memory only)."""
        self._stats_prefs[user_key] = enabled
        logger.debug("Stats preference for %r set to %s", user_key, enabled)
