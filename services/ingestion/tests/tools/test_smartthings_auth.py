"""Tests for SmartThings token storage: atomic writes, and the on-disk
schema (expires_at, not expires_in) that get_valid_access_token relies on."""

import json
from datetime import datetime, timezone

import pytest

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


def test_save_token_creates_parent_directory(tmp_path):
    token_path = tmp_path / "nested" / "dir" / "smartthings_token.json"
    auth.save_token(token_path, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
    assert token_path.exists()
