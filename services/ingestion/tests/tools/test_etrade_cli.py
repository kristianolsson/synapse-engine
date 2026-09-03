"""Unit tests for etrade_cli's authentication fallback logic: an
unattended (headless) run that fails automated login should fall back to
the manual PIN-auth prompt instead of just erroring out; a --headed run
(a human already watching) should not."""

import json
import time

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


@pytest.fixture(autouse=True)
def telegram_configured(monkeypatch):
    """Most tests below exercise the Telegram (button-deferred) path —
    configure it by default and stub the actual send so no network call
    happens. Individual tests override chat_id/config as needed."""
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [123])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "")
    monkeypatch.setattr(
        "services.ingestion.channels.telegram.sender.send_telegram_message",
        lambda chat_id, text, reply_markup=None: 999,
    )


def test_headless_failure_falls_back_and_sends_telegram_button_with_no_url(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_pending"

    pending = etrade_pin_auth.load_pending()
    assert pending["activated"] is False
    assert "authorize_url" not in pending
    assert pending["channel"] == "telegram"
    assert pending["chat_id"] == 123
    assert pending["prompt_message_id"] == 999


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


def test_headless_failure_via_email_starts_pin_auth_immediately(monkeypatch, capsys):
    """Email has no interactive buttons, so it keeps the immediate-URL
    behavior — the real request token is still fetched eagerly."""
    import services.ingestion.config as real_config
    monkeypatch.setattr(real_config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(real_config, "TELEGRAM_ALLOWED_USER_IDS", [])
    monkeypatch.setattr(real_config, "REPLY_TO_ADDRESS", "user@example.com")

    def mock_start_pin_auth(key, secret):
        pending = {"oauth_token": "T", "oauth_token_secret": "S", "authorize_url": "https://x/y", "created_at": time.time(), "activated": True, "retries": []}
        etrade_pin_auth.PENDING_FILE.write_text(json.dumps(pending))
        return pending

    monkeypatch.setattr(etrade_pin_auth, "start_pin_auth", mock_start_pin_auth)
    monkeypatch.setattr("services.ingestion.channels.email.reply.send_reply", lambda **kwargs: True)

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_pending"
    pending = etrade_pin_auth.load_pending()
    assert pending["authorize_url"] == "https://x/y"
    assert pending["channel"] == "email"


def test_headed_failure_does_not_attempt_pin_fallback(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(etrade_pin_auth, "create_pending_request", lambda *a, **k: called.append(1))

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=False)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"
    assert not called


def test_headless_failure_with_pending_already_in_flight_queues_retry(monkeypatch, capsys):
    etrade_pin_auth.PENDING_FILE.write_text(
        json.dumps({"created_at": time.time(), "activated": False, "retries": [], "channel": "telegram", "chat_id": 123, "prompt_message_id": 999})
    )
    created = []
    monkeypatch.setattr(etrade_pin_auth, "create_pending_request", lambda *a, **k: created.append(1))
    monkeypatch.setenv("SYNAPSE_REMINDER_TASK", "Run options-bot scan --tickers AAPL")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "telegram")
    monkeypatch.delenv("SYNAPSE_SESSION_KEY", raising=False)

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_pending"
    assert "queued" in out["error"]
    assert not created  # joined the existing pending instead of starting a second one

    pending = etrade_pin_auth.load_pending()
    assert pending["retries"] == [{"reminder_task": "Run options-bot scan --tickers AAPL", "retry_channel": "telegram"}]
    # The original button/message is untouched — no second prompt sent.
    assert pending["prompt_message_id"] == 999


def test_headless_failure_with_pending_already_in_flight_and_no_correlation_reports_already_pending(monkeypatch, capsys):
    """A dispatch path with no SYNAPSE_* env (e.g. a bare manual CLI run
    outside the assistant pipeline) has nothing to queue — keep the
    original "reply to it" message instead of claiming something was
    queued."""
    etrade_pin_auth.PENDING_FILE.write_text(
        json.dumps({"created_at": time.time(), "activated": False, "retries": []})
    )
    for var in ("SYNAPSE_SESSION_KEY", "SYNAPSE_REMINDER_TASK"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_pending"
    assert "already pending" in out["error"]
    assert etrade_pin_auth.load_pending()["retries"] == []


def test_headless_failure_captures_session_correlation_for_retry(monkeypatch, capsys):
    monkeypatch.setenv("SYNAPSE_SESSION_KEY", "user-1")
    monkeypatch.setenv("SYNAPSE_SESSION_ID", "sess-abc")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "telegram")
    monkeypatch.setenv("SYNAPSE_CHAT_ID", "555")
    monkeypatch.setattr("sys.argv", ["etrade", "balance"])

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    entry = pending["retries"][0]
    assert entry["session_key"] == "user-1"
    assert entry["session_id"] == "sess-abc"
    assert entry["retry_channel"] == "telegram"
    assert entry["retry_chat_id"] == 555
    assert entry["failed_command"] == "etrade balance"


def test_headless_failure_normalizes_full_script_path_in_failed_command(monkeypatch, capsys):
    """Production argv[0] is the full interpreter path (e.g.
    /app/synapse-engine/services/ingestion/tools/etrade_cli.py), not the
    clean "etrade"/"options-bot" name other tests mock — failed_command
    must normalize it, since a raw absolute path echoed back into the
    retry prompt reads as an attempt to redirect tool use."""
    monkeypatch.setenv("SYNAPSE_SESSION_KEY", "user-1")
    monkeypatch.setenv("SYNAPSE_SESSION_ID", "sess-abc")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "telegram")
    monkeypatch.setenv("SYNAPSE_CHAT_ID", "555")
    monkeypatch.setattr(
        "sys.argv", ["/app/synapse-engine/services/ingestion/tools/etrade_cli.py", "balance"]
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    assert pending["retries"][0]["failed_command"] == "etrade balance"


def test_headless_failure_normalizes_options_bot_script_path(monkeypatch, capsys):
    monkeypatch.setenv("SYNAPSE_SESSION_KEY", "user-1")
    monkeypatch.setenv("SYNAPSE_SESSION_ID", "sess-abc")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "telegram")
    monkeypatch.setenv("SYNAPSE_CHAT_ID", "555")
    monkeypatch.setattr(
        "sys.argv",
        ["/app/synapse-engine/services/ingestion/tools/options_bot_cli.py", "scan", "--tickers", "AAPL"],
    )

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    assert pending["retries"][0]["failed_command"] == "options-bot scan --tickers AAPL"


def test_headless_failure_captures_email_correlation_for_retry(monkeypatch, capsys):
    monkeypatch.setenv("SYNAPSE_SESSION_KEY", "<thread@synapse.local>")
    monkeypatch.setenv("SYNAPSE_SESSION_ID", "")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "email")
    monkeypatch.setenv("SYNAPSE_EMAIL_TO", "user@example.com")
    monkeypatch.setenv("SYNAPSE_EMAIL_SUBJECT", "Check my balance")
    monkeypatch.setenv("SYNAPSE_EMAIL_MESSAGE_ID", "<orig@example.com>")
    monkeypatch.setenv("SYNAPSE_EMAIL_REFERENCES", "<orig@example.com>")
    monkeypatch.setattr("sys.argv", ["etrade", "balance"])

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    entry = pending["retries"][0]
    assert entry["session_key"] == "<thread@synapse.local>"
    assert entry["session_id"] == ""
    assert entry["retry_channel"] == "email"
    assert entry["email_to"] == "user@example.com"
    assert entry["email_subject"] == "Check my balance"
    assert entry["email_message_id"] == "<orig@example.com>"
    assert entry["email_references"] == "<orig@example.com>"


def test_headless_failure_captures_reminder_task_for_replay(monkeypatch, capsys):
    monkeypatch.setenv("SYNAPSE_REMINDER_TASK", "Run options-bot scan --tickers AAPL,MSFT")
    monkeypatch.setenv("SYNAPSE_CHANNEL", "email")
    monkeypatch.delenv("SYNAPSE_SESSION_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["options-bot", "scan", "--tickers", "AAPL,MSFT"])

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    entry = pending["retries"][0]
    assert entry["reminder_task"] == "Run options-bot scan --tickers AAPL,MSFT"
    assert entry["retry_channel"] == "email"
    assert "session_key" not in entry


def test_headless_failure_with_no_synapse_env_captures_no_retry_fields(monkeypatch, capsys):
    """/update-etrade-auth and any other dispatch path that doesn't set
    SYNAPSE_* env vars must produce a pending record with nothing to
    retry."""
    for var in ("SYNAPSE_SESSION_KEY", "SYNAPSE_REMINDER_TASK", "SYNAPSE_CHANNEL"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(SystemExit):
        etrade_cli._authenticate(ENV, headless=True)

    pending = etrade_pin_auth.load_pending()
    assert pending["retries"] == []
