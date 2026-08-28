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
