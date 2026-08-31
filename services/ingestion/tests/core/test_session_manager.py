"""Tests for the session manager."""

import json
import os
import tempfile
import time
from unittest.mock import patch


from services.ingestion.core.session_manager import SessionManager, UserSession


class TestSessionManager:
    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(self.fd)
        self.sm = SessionManager(filepath=self.path, ttl_minutes=60)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_save_and_get_session(self):
        self.setUp()
        self.sm.save_session("user1", "session-abc")

        sid = self.sm.get_session("user1")
        assert sid == "session-abc"

        # Another user should be None
        assert self.sm.get_session("user2") is None
        self.tearDown()

    def test_clear_session(self):
        self.setUp()
        self.sm.save_session("user1", "session-abc")

        assert self.sm.clear_session("user1") is True
        assert self.sm.get_session("user1") is None

        # Clearing an empty session returns False
        assert self.sm.clear_session("user2") is False
        self.tearDown()

    def test_ttl_expiration(self):
        self.setUp()
        self.sm.save_session("user1", "session-abc")

        # Manually alter the file to make it artificially old
        with open(self.path, "r") as f:
            data = json.load(f)
        # Key is prefixed with the provider name (e.g. "gemini:user1")
        prefixed_key = self.sm._provider_key("user1")
        data[prefixed_key]["last_seen"] = time.time() - 4000  # Older than 3600s (60 mins)
        with open(self.path, "w") as f:
            json.dump(data, f)

        # Retrieval should return None and clean it up
        assert self.sm.get_session("user1") is None
        self.tearDown()

    def _set_last_seen(self, key: str, ts: float) -> None:
        with open(self.path, "r") as f:
            data = json.load(f)
        prefixed_key = self.sm._provider_key(key)
        data[prefixed_key]["last_seen"] = ts
        with open(self.path, "w") as f:
            json.dump(data, f)

    def test_midnight_reset_purges_default_session(self):
        # A generous TTL so only the midnight-reset branch (not plain TTL
        # expiry) can be responsible for the purge.
        self.fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(self.fd)
        self.sm = SessionManager(filepath=self.path, ttl_minutes=1500)

        # 2026-08-29 23:59:00 local
        last_seen = time.mktime((2026, 8, 29, 23, 59, 0, 0, 0, -1))
        # 2026-08-30 00:01:00 local — 2 minutes later, different calendar day
        now = time.mktime((2026, 8, 30, 0, 1, 0, 0, 0, -1))

        self.sm.save_session("user1", "session-abc")
        self._set_last_seen("user1", last_seen)

        with patch("services.ingestion.core.session_manager.time.time", return_value=now):
            assert self.sm.get_session("user1") is None
        self.tearDown()

    def test_daily_reset_false_survives_midnight(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(self.fd)
        self.sm = SessionManager(filepath=self.path, ttl_minutes=1500)

        last_seen = time.mktime((2026, 8, 29, 23, 59, 0, 0, 0, -1))
        now = time.mktime((2026, 8, 30, 0, 1, 0, 0, 0, -1))

        self.sm.save_session("thread-1", "session-abc", daily_reset=False)
        self._set_last_seen("thread-1", last_seen)

        with patch("services.ingestion.core.session_manager.time.time", return_value=now):
            assert self.sm.get_session("thread-1") == "session-abc"
        self.tearDown()

    def test_message_session_survives_midnight(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(self.fd)
        self.sm = SessionManager(filepath=self.path, ttl_minutes=1500)

        last_seen = time.mktime((2026, 8, 29, 23, 59, 0, 0, 0, -1))
        now = time.mktime((2026, 8, 30, 0, 1, 0, 0, 0, -1))

        self.sm.save_message_session(999, "session-abc")
        self._set_last_seen("msg_999", last_seen)

        with patch("services.ingestion.core.session_manager.time.time", return_value=now):
            assert self.sm.get_message_session(999) == "session-abc"
        self.tearDown()


class TestStatsPreference:
    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(self.fd)
        self.sm = SessionManager(filepath=self.path, ttl_minutes=60)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    @patch("services.ingestion.core.session_manager.config")
    def test_defaults_to_config(self, mock_config):
        self.setUp()
        mock_config.STATS_ENABLED = False
        assert self.sm.get_stats_enabled("user1") is False

        mock_config.STATS_ENABLED = True
        assert self.sm.get_stats_enabled("user1") is True
        self.tearDown()

    @patch("services.ingestion.core.session_manager.config")
    def test_per_user_override(self, mock_config):
        self.setUp()
        mock_config.STATS_ENABLED = False

        self.sm.set_stats_enabled("user1", True)
        assert self.sm.get_stats_enabled("user1") is True

        # Other users still use default
        assert self.sm.get_stats_enabled("user2") is False

        self.sm.set_stats_enabled("user1", False)
        assert self.sm.get_stats_enabled("user1") is False
        self.tearDown()


class TestUserSession:
    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(self.fd)
        self.sm = SessionManager(filepath=self.path, ttl_minutes=60)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_session_id_and_save_delegate_to_manager_by_key(self):
        self.setUp()
        session = UserSession(self.sm, "user1")
        assert session.session_id is None

        session.save("sess-abc")
        assert session.session_id == "sess-abc"
        assert self.sm.get_session("user1") == "sess-abc"
        self.tearDown()

    def test_daily_reset_false_passed_through_to_manager(self):
        # Email threads pass daily_reset=False so a reply days later still
        # resumes the same session (see save_session's docstring).
        self.setUp()
        session = UserSession(self.sm, "<thread-1@synapse.local>", daily_reset=False)
        session.save("sess-abc")

        with open(self.path, "r") as f:
            data = json.load(f)
        prefixed_key = self.sm._provider_key("<thread-1@synapse.local>")
        assert data[prefixed_key]["daily_reset"] is False
        self.tearDown()

    def test_clear_delegates_to_manager(self):
        self.setUp()
        session = UserSession(self.sm, "user1")
        session.save("sess-abc")

        assert session.clear() is True
        assert session.session_id is None
        self.tearDown()

    @patch("services.ingestion.core.session_manager.config")
    def test_stats_key_defaults_to_key(self, mock_config):
        self.setUp()
        mock_config.STATS_ENABLED = False
        session = UserSession(self.sm, "user1")

        session.set_stats_enabled(True)
        assert session.stats_enabled is True
        assert self.sm.get_stats_enabled("user1") is True
        self.tearDown()

    @patch("services.ingestion.core.session_manager.config")
    def test_stats_key_can_differ_from_key(self, mock_config):
        # Email threads: session continuity is per-thread (`key`), but the
        # /stats preference is per-sender (`stats_key`) — one person can
        # have many threads.
        self.setUp()
        mock_config.STATS_ENABLED = False
        session = UserSession(self.sm, "<thread-1@synapse.local>", stats_key="user@example.com")

        session.set_stats_enabled(True)
        assert session.stats_enabled is True
        assert self.sm.get_stats_enabled("user@example.com") is True
        # The session-continuity key is untouched by the stats toggle.
        assert self.sm.get_stats_enabled("<thread-1@synapse.local>") is False
        self.tearDown()
