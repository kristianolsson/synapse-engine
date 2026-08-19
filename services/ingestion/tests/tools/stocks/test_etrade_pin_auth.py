import json
import time
from unittest.mock import MagicMock, patch

import pytest

from services.ingestion.tools.stocks import etrade_pin_auth


@pytest.fixture(autouse=True)
def pending_file(tmp_path, monkeypatch):
    path = tmp_path / ".etrade_pending_auth.json"
    monkeypatch.setattr(etrade_pin_auth, "PENDING_FILE", path)
    return path


@pytest.fixture(autouse=True)
def token_file(tmp_path, monkeypatch):
    path = tmp_path / ".etrade_tokens"
    monkeypatch.setattr(etrade_pin_auth, "TOKEN_FILE", path)
    return path


def test_load_pending_returns_none_when_no_file():
    assert etrade_pin_auth.load_pending() is None


def test_load_pending_returns_none_and_clears_when_expired(pending_file):
    pending_file.write_text(
        '{"oauth_token": "t", "oauth_token_secret": "s", "created_at": %f}'
        % (time.time() - etrade_pin_auth.PENDING_TTL_SECONDS - 1)
    )

    result = etrade_pin_auth.load_pending()

    assert result is None
    assert not pending_file.exists()


def test_load_pending_returns_data_when_fresh(pending_file):
    pending_file.write_text('{"oauth_token": "t", "oauth_token_secret": "s", "created_at": %f}' % time.time())

    result = etrade_pin_auth.load_pending()

    assert result == {"oauth_token": "t", "oauth_token_secret": "s", "created_at": result["created_at"]}


def test_load_pending_returns_none_on_corrupt_file(pending_file):
    pending_file.write_text("not json")

    assert etrade_pin_auth.load_pending() is None


def test_clear_pending_removes_file(pending_file):
    pending_file.write_text("{}")

    etrade_pin_auth.clear_pending()

    assert not pending_file.exists()


def test_clear_pending_is_noop_when_missing():
    etrade_pin_auth.clear_pending()  # must not raise


def test_start_pin_auth_writes_pending_state(pending_file):
    fake_session = MagicMock()
    fake_session.fetch_request_token.return_value = {
        "oauth_token": "REQ_TOKEN",
        "oauth_token_secret": "REQ_SECRET",
    }

    with patch.object(etrade_pin_auth, "OAuth1Session", return_value=fake_session):
        pending = etrade_pin_auth.start_pin_auth("consumer_key", "consumer_secret")

    assert pending["oauth_token"] == "REQ_TOKEN"
    assert pending["oauth_token_secret"] == "REQ_SECRET"
    assert pending["authorize_url"] == "https://us.etrade.com/e/t/etws/authorize?key=consumer_key&token=REQ_TOKEN"
    assert "created_at" in pending

    on_disk = etrade_pin_auth.load_pending()
    assert on_disk["oauth_token"] == "REQ_TOKEN"


def test_mark_prompt_sent_merges_fields_into_pending(pending_file):
    pending_file.write_text('{"oauth_token": "t", "oauth_token_secret": "s", "created_at": %f}' % time.time())

    etrade_pin_auth.mark_prompt_sent(channel="telegram", chat_id=123, prompt_message_id=42)

    on_disk = etrade_pin_auth.load_pending()
    assert on_disk["channel"] == "telegram"
    assert on_disk["chat_id"] == 123
    assert on_disk["prompt_message_id"] == 42
    assert on_disk["oauth_token"] == "t"


def test_mark_prompt_sent_is_noop_when_no_pending_request():
    etrade_pin_auth.mark_prompt_sent(channel="telegram")  # must not raise
    assert etrade_pin_auth.load_pending() is None


def test_finish_pin_auth_returns_access_token():
    fake_session = MagicMock()
    fake_session.token = {"oauth_token": "ACCESS_TOKEN", "oauth_token_secret": "ACCESS_SECRET"}
    pending = {"oauth_token": "REQ_TOKEN", "oauth_token_secret": "REQ_SECRET"}

    with patch.object(etrade_pin_auth, "OAuth1Session", return_value=fake_session) as session_cls:
        result = etrade_pin_auth.finish_pin_auth(pending, "  123456  ", "consumer_key", "consumer_secret")

    session_cls.assert_called_once_with(
        client_id="consumer_key",
        client_secret="consumer_secret",
        token="REQ_TOKEN",
        token_secret="REQ_SECRET",
        redirect_uri="oob",
    )
    fake_session.fetch_access_token.assert_called_once_with(
        url="https://api.etrade.com/oauth/access_token", verifier="123456"
    )
    assert result == {"access_token": "ACCESS_TOKEN", "access_token_secret": "ACCESS_SECRET"}


def test_save_access_token_writes_expected_schema(token_file):
    etrade_pin_auth.save_access_token("ACCESS_TOKEN", "ACCESS_SECRET", sandbox=False)

    data = json.loads(token_file.read_text())
    assert data == {"access_token": "ACCESS_TOKEN", "access_token_secret": "ACCESS_SECRET", "sandbox": False}
    assert oct(token_file.stat().st_mode)[-3:] == "600"
