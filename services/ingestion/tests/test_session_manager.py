"""Tests for the session manager."""

import json
import os
import tempfile
import time
from unittest.mock import patch, MagicMock

import pytest

from services.ingestion.session_manager import SessionManager


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
        data["user1"]["last_seen"] = time.time() - 4000  # Older than 3600s (60 mins)
        with open(self.path, "w") as f:
            json.dump(data, f)
            
        # Retrieval should return None and clean it up
        assert self.sm.get_session("user1") is None
        self.tearDown()
