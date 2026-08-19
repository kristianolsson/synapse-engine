"""Unit tests for etrade_cli's authentication fallback logic: an
unattended (headless) run that fails automated login should fall back to
the manual PIN-auth prompt instead of just erroring out; a --headed run
(a human already watching) should not."""

import json

import pytest

from services.ingestion.tools import etrade_cli
from services.ingestion.tools.stocks import etrade_pin_auth

ENV = {
    "consumer_key": "key",
    "consumer_secret": "secret",
    "username": "",
    "password": "",
    "totp_secret": "",
    "mode": "production",
}


@pytest.fixture(autouse=True)
def pending_file(tmp_path, monkeypatch):
    path = tmp_path / ".etrade_pending_auth.json"
    monkeypatch.setattr(etrade_pin_auth, "PENDING_FILE", path)
    return path


@pytest.fixture(autouse=True)
def no_wetrade(monkeypatch):
    """Force the ETradeAuth (manual PIN) branch so tests don't need
    Playwright — authenticate() just raises to simulate a failure."""
    monkeypatch.setattr(etrade_cli, "WETRADE_AVAILABLE", False)

    def _raise(self, headless=True):
        raise RuntimeError("simulated auth failure")

    monkeypatch.setattr(etrade_cli.ETradeAuth, "authenticate", _raise)


def test_headless_failure_falls_back_and_starts_pin_auth_via_telegram(monkeypatch, capsys):
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [123])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "")

    monkeypatch.setattr(
        etrade_pin_auth,
        "start_pin_auth",
        lambda key, secret: {"oauth_token": "T", "oauth_token_secret": "S", "authorize_url": "https://x/y"},
    )
    monkeypatch.setattr(
        "services.ingestion.channels.telegram.sender.send_telegram_message",
        lambda chat_id, text: 999,
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_pending"


def test_headless_failure_with_no_channels_configured_reports_auth_failed(monkeypatch, capsys):
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "")

    monkeypatch.setattr(
        etrade_pin_auth,
        "start_pin_auth",
        lambda key, secret: {"oauth_token": "T", "oauth_token_secret": "S", "authorize_url": "https://x/y"},
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"
    assert etrade_pin_auth.load_pending() is None  # cleared, nothing to reply to


def test_headed_failure_does_not_attempt_pin_fallback(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(etrade_pin_auth, "start_pin_auth", lambda *a, **k: called.append(1))

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=False)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"
    assert not called


def test_headless_failure_with_pending_already_in_flight_reports_auth_pending(monkeypatch, capsys):
    etrade_pin_auth.PENDING_FILE.write_text(
        json.dumps({"oauth_token": "T", "oauth_token_secret": "S", "created_at": __import__("time").time()})
    )
    started = []
    monkeypatch.setattr(etrade_pin_auth, "start_pin_auth", lambda *a, **k: started.append(1))

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_pending"
    assert "already pending" in out["error"]
    assert not started


def test_headless_failure_captures_session_correlation_for_retry(monkeypatch, capsys):
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [123])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "")
    monkeypatch.setenv("SYNAPSE_SESSION_KEY", "user-1")
    monkeypatch.setenv("SYNAPSE_SESSION_ID", "sess-abc")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "telegram")
    monkeypatch.setenv("SYNAPSE_CHAT_ID", "555")
    monkeypatch.setattr("sys.argv", ["etrade", "balance"])

    def mock_start_pin_auth(key, secret):
        pending = {"oauth_token": "T", "oauth_token_secret": "S", "authorize_url": "https://x/y", "created_at": __import__("time").time()}
        etrade_pin_auth.PENDING_FILE.write_text(json.dumps(pending))
        return pending

    monkeypatch.setattr(etrade_pin_auth, "start_pin_auth", mock_start_pin_auth)
    monkeypatch.setattr(
        "services.ingestion.channels.telegram.sender.send_telegram_message",
        lambda chat_id, text: 999,
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    assert pending["session_key"] == "user-1"
    assert pending["session_id"] == "sess-abc"
    assert pending["retry_channel"] == "telegram"
    assert pending["chat_id"] == 555
    assert pending["failed_command"] == "etrade balance"


def test_headless_failure_captures_email_correlation_for_retry(monkeypatch, capsys):
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [123])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "")
    monkeypatch.setenv("SYNAPSE_SESSION_KEY", "<thread@synapse.local>")
    monkeypatch.setenv("SYNAPSE_SESSION_ID", "")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "email")
    monkeypatch.setenv("SYNAPSE_EMAIL_TO", "user@example.com")
    monkeypatch.setenv("SYNAPSE_EMAIL_SUBJECT", "Check my balance")
    monkeypatch.setenv("SYNAPSE_EMAIL_MESSAGE_ID", "<orig@example.com>")
    monkeypatch.setenv("SYNAPSE_EMAIL_REFERENCES", "<orig@example.com>")
    monkeypatch.setattr("sys.argv", ["etrade", "balance"])

    def mock_start_pin_auth(key, secret):
        pending = {"oauth_token": "T", "oauth_token_secret": "S", "authorize_url": "https://x/y", "created_at": __import__("time").time()}
        etrade_pin_auth.PENDING_FILE.write_text(json.dumps(pending))
        return pending

    monkeypatch.setattr(etrade_pin_auth, "start_pin_auth", mock_start_pin_auth)
    monkeypatch.setattr(
        "services.ingestion.channels.telegram.sender.send_telegram_message",
        lambda chat_id, text: 999,
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    assert pending["session_key"] == "<thread@synapse.local>"
    assert pending["session_id"] == ""
    assert pending["retry_channel"] == "email"
    assert pending["email_to"] == "user@example.com"
    assert pending["email_subject"] == "Check my balance"
    assert pending["email_message_id"] == "<orig@example.com>"
    assert pending["email_references"] == "<orig@example.com>"


def test_headless_failure_captures_reminder_task_for_replay(monkeypatch, capsys):
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [123])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "")
    monkeypatch.setenv("SYNAPSE_REMINDER_TASK", "Run options-bot scan --tickers AAPL,MSFT")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "email")
    monkeypatch.delenv("SYNAPSE_SESSION_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["options-bot", "scan", "--tickers", "AAPL,MSFT"])

    def mock_start_pin_auth(key, secret):
        pending = {"oauth_token": "T", "oauth_token_secret": "S", "authorize_url": "https://x/y", "created_at": __import__("time").time()}
        etrade_pin_auth.PENDING_FILE.write_text(json.dumps(pending))
        return pending

    monkeypatch.setattr(etrade_pin_auth, "start_pin_auth", mock_start_pin_auth)
    monkeypatch.setattr(
        "services.ingestion.channels.telegram.sender.send_telegram_message",
        lambda chat_id, text: 999,
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    assert pending["reminder_task"] == "Run options-bot scan --tickers AAPL,MSFT"
    assert pending["retry_channel"] == "email"
    assert "session_key" not in pending


def test_headless_failure_with_no_synapse_env_captures_no_retry_fields(monkeypatch, capsys):
    """/update-etrade-auth and any other dispatch path that doesn't set
    SYNAPSE_* env vars must produce a pending record with nothing to
    retry."""
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [123])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "")
    for var in ("SYNAPSE_SESSION_KEY", "SYNAPSE_REMINDER_TASK", "SYNAPSE_CHANNEL"):
        monkeypatch.delenv(var, raising=False)

    def mock_start_pin_auth(key, secret):
        pending = {"oauth_token": "T", "oauth_token_secret": "S", "authorize_url": "https://x/y", "created_at": __import__("time").time()}
        etrade_pin_auth.PENDING_FILE.write_text(json.dumps(pending))
        return pending

    monkeypatch.setattr(etrade_pin_auth, "start_pin_auth", mock_start_pin_auth)
    monkeypatch.setattr(
        "services.ingestion.channels.telegram.sender.send_telegram_message",
        lambda chat_id, text: 999,
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    assert "session_key" not in pending
    assert "reminder_task" not in pending
