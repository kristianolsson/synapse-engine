"""Tests for SmartThings token storage: atomic writes, and the on-disk
schema (expires_at, not expires_in) that get_valid_access_token relies on."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from services.ingestion.tools.smartthings import auth


def test_load_token_returns_none_when_file_missing(tmp_path):
    assert auth.load_token(tmp_path / "missing.json") is None


def test_save_then_load_round_trips_and_converts_expires_in_to_expires_at(tmp_path):
    token_path = tmp_path / "smartthings_token.json"
    before = datetime.now(timezone.utc)

    auth.save_token(token_path, {
        "access_token": "AT-1",
        "refresh_token": "RT-1",
        "expires_in": 86400,
    })

    loaded = auth.load_token(token_path)
    assert loaded["access_token"] == "AT-1"
    assert loaded["refresh_token"] == "RT-1"
    expires_at = datetime.fromisoformat(loaded["expires_at"])
    assert expires_at > before


def test_save_token_writes_atomically_leaving_no_tmp_file(tmp_path):
    token_path = tmp_path / "smartthings_token.json"
    auth.save_token(token_path, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    assert token_path.exists()
    assert not token_path.with_suffix(".json.tmp").exists()
    with open(token_path) as f:
        data = json.load(f)
    assert set(data.keys()) == {"access_token", "refresh_token", "expires_at"}


def test_save_token_writes_file_with_0600_permissions(tmp_path):
    token_path = tmp_path / "smartthings_token.json"
    auth.save_token(token_path, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    assert oct(token_path.stat().st_mode)[-3:] == "600"


def test_save_token_creates_parent_directory(tmp_path):
    token_path = tmp_path / "nested" / "dir" / "smartthings_token.json"
    auth.save_token(token_path, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
    assert token_path.exists()


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text or json.dumps(self._json_body)

    def json(self):
        return self._json_body


def test_build_authorize_url_includes_required_params():
    url = auth.build_authorize_url("client-123", "http://localhost:8765/callback", "state-abc")
    assert url.startswith("https://api.smartthings.com/oauth/authorize?")
    assert "client_id=client-123" in url
    assert "state=state-abc" in url
    assert "response_type=code" in url


def test_exchange_code_success_returns_token_response(monkeypatch):
    def fake_post(url, data, auth, timeout):
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "the-code"
        return _FakeResponse(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 86400})

    monkeypatch.setattr(requests, "post", fake_post)
    result = auth.exchange_code("cid", "csecret", "http://localhost:8765/callback", "the-code")
    assert result["access_token"] == "AT"


def test_exchange_code_failure_raises_auth_error(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResponse(400, text="bad request"))
    with pytest.raises(auth.SmartThingsAuthError):
        auth.exchange_code("cid", "csecret", "http://localhost:8765/callback", "bad-code")


def test_get_valid_access_token_returns_cached_token_when_not_expired(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    token_path.write_text(json.dumps({"access_token": "AT-cached", "refresh_token": "RT", "expires_at": future}))

    monkeypatch.setattr(requests, "post", lambda *a, **kw: pytest.fail("should not refresh"))
    assert auth.get_valid_access_token(token_path, "cid", "csecret") == "AT-cached"


def test_get_valid_access_token_refreshes_when_expired_and_saves_rotated_token(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    token_path.write_text(json.dumps({"access_token": "AT-old", "refresh_token": "RT-old", "expires_at": past}))

    def fake_post(url, data, auth, timeout):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "RT-old"
        return _FakeResponse(200, {"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 86400})

    monkeypatch.setattr(requests, "post", fake_post)
    result = auth.get_valid_access_token(token_path, "cid", "csecret")

    assert result == "AT-new"
    saved = json.loads(token_path.read_text())
    assert saved["refresh_token"] == "RT-new"  # rotated refresh_token persisted immediately


def test_get_valid_access_token_raises_when_no_token_exists(tmp_path):
    with pytest.raises(auth.SmartThingsAuthError, match="run 'smartthings auth'"):
        auth.get_valid_access_token(tmp_path / "missing.json", "cid", "csecret")


def test_get_valid_access_token_fails_loud_on_revoked_refresh_token(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    token_path.write_text(json.dumps({"access_token": "AT-old", "refresh_token": "RT-revoked", "expires_at": past}))

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResponse(400, text="invalid_grant"))
    with pytest.raises(auth.SmartThingsAuthError, match="reauthorize"):
        auth.get_valid_access_token(token_path, "cid", "csecret")


def test_get_valid_access_token_preserves_original_refresh_failure_detail(tmp_path, monkeypatch):
    """The wrapped SmartThingsAuthError must not discard the original
    refresh failure's status/message — otherwise a transient 5xx gets
    misreported identically to a permanent revocation."""
    token_path = tmp_path / "token.json"
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    token_path.write_text(json.dumps({"access_token": "AT-old", "refresh_token": "RT-old", "expires_at": past}))

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _FakeResponse(503, text="upstream unavailable"))
    with pytest.raises(auth.SmartThingsAuthError) as exc:
        auth.get_valid_access_token(token_path, "cid", "csecret")

    message = str(exc.value)
    assert "503" in message
    assert "upstream unavailable" in message
    assert "reauthorize" in message
