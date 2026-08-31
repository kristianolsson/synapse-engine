import json
import time
from unittest.mock import ANY, MagicMock, patch

import pytest

from services.ingestion.tools.stocks import etrade_pin_auth


@pytest.fixture(autouse=True)
def pending_file(tmp_path, monkeypatch):
    path = tmp_path / ".etrade_pending_auth.json"
    monkeypatch.setattr(etrade_pin_auth, "PENDING_FILE", path)
    monkeypatch.setattr(etrade_pin_auth, "PENDING_LOCK_FILE", tmp_path / ".etrade_pending_auth.json.lock")
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


def test_claim_pending_returns_and_removes_data_when_fresh(pending_file):
    pending_file.write_text('{"oauth_token": "t", "oauth_token_secret": "s", "created_at": %f}' % time.time())

    result = etrade_pin_auth.claim_pending()

    assert result == {"oauth_token": "t", "oauth_token_secret": "s", "created_at": result["created_at"]}
    assert not pending_file.exists()


def test_claim_pending_returns_none_when_no_file():
    assert etrade_pin_auth.claim_pending() is None


def test_claim_pending_second_call_returns_none():
    """The concurrency guarantee this exists for: of two callers racing to
    complete the same pending request, only the first can win."""
    etrade_pin_auth.PENDING_FILE.write_text(
        '{"oauth_token": "t", "oauth_token_secret": "s", "created_at": %f}' % time.time()
    )

    first = etrade_pin_auth.claim_pending()
    second = etrade_pin_auth.claim_pending()

    assert first is not None
    assert second is None


def test_restore_pending_allows_a_later_claim_to_succeed(pending_file):
    """The failed-code-then-correct-code case: a claim that fails the OAuth
    exchange restores the request, and a follow-up reply can still claim
    and complete it — the correlation fields (e.g. reminder_task) survive
    the round trip untouched."""
    pending = {
        "oauth_token": "t",
        "oauth_token_secret": "s",
        "created_at": time.time(),
        "reminder_task": "Run options-bot scan --tickers AAPL",
    }
    claimed = pending  # simulates what the listener got from its own claim_pending()

    etrade_pin_auth.restore_pending(claimed)

    assert pending_file.exists()
    second_claim = etrade_pin_auth.claim_pending()
    assert second_claim == pending
    assert not pending_file.exists()


def test_claim_pending_returns_none_when_expired(pending_file):
    pending_file.write_text(
        '{"oauth_token": "t", "oauth_token_secret": "s", "created_at": %f}'
        % (time.time() - etrade_pin_auth.PENDING_TTL_SECONDS - 1)
    )

    assert etrade_pin_auth.claim_pending() is None
    assert not pending_file.exists()


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


def test_backfill_session_id_fills_empty_session_id_when_key_matches(pending_file):
    pending_file.write_text(json.dumps({
        "oauth_token": "t", "oauth_token_secret": "s", "created_at": time.time(),
        "session_key": "user-1", "session_id": "",
    }))

    etrade_pin_auth.backfill_session_id("user-1", "sess-abc")

    assert etrade_pin_auth.load_pending()["session_id"] == "sess-abc"


def test_backfill_session_id_noop_when_key_does_not_match(pending_file):
    pending_file.write_text(json.dumps({
        "oauth_token": "t", "oauth_token_secret": "s", "created_at": time.time(),
        "session_key": "user-1", "session_id": "",
    }))

    etrade_pin_auth.backfill_session_id("user-2", "sess-abc")

    assert etrade_pin_auth.load_pending()["session_id"] == ""


def test_backfill_session_id_noop_when_already_set(pending_file):
    pending_file.write_text(json.dumps({
        "oauth_token": "t", "oauth_token_secret": "s", "created_at": time.time(),
        "session_key": "user-1", "session_id": "sess-original",
    }))

    etrade_pin_auth.backfill_session_id("user-1", "sess-new")

    assert etrade_pin_auth.load_pending()["session_id"] == "sess-original"


def test_backfill_session_id_noop_when_new_session_id_empty(pending_file):
    pending_file.write_text(json.dumps({
        "oauth_token": "t", "oauth_token_secret": "s", "created_at": time.time(),
        "session_key": "user-1", "session_id": "",
    }))

    etrade_pin_auth.backfill_session_id("user-1", "")

    assert etrade_pin_auth.load_pending()["session_id"] == ""


def test_backfill_session_id_noop_when_no_pending_request():
    etrade_pin_auth.backfill_session_id("user-1", "sess-abc")  # must not raise
    assert etrade_pin_auth.load_pending() is None


def test_complete_and_maybe_retry_resumes_session_and_delivers_via_telegram():
    pending = {
        "session_key": "user-1", "session_id": "sess-abc",
        "failed_command": "etrade balance", "retry_channel": "telegram", "chat_id": 555,
    }
    fake_result = MagicMock(session_id="sess-new", output="Your balance is $1,000", stats={"cost": 0.01})
    mock_sm = MagicMock()

    with patch("services.ingestion.core.pipe.pipe_to_provider", return_value=fake_result) as mock_pipe, \
         patch("services.ingestion.channels.telegram.sender.send_telegram_message") as mock_send:
        etrade_pin_auth.complete_and_maybe_retry(pending, mock_sm)

    mock_pipe.assert_called_once_with(ANY, session_id="sess-abc")
    retry_prompt = mock_pipe.call_args.args[0]
    assert "etrade balance" in retry_prompt
    assert "complete my full original request" in retry_prompt
    # Must arrive wrapped in the same envelope every real message gets —
    # a bare unattributed instruction reads as a prompt injection attempt.
    assert "Type: telegram" in retry_prompt
    assert "Sender: user-1" in retry_prompt
    mock_sm.save_session.assert_called_once_with("user-1", "sess-new", daily_reset=True)
    # The raw stats dict and a UserSession handle are forwarded as-is —
    # send_telegram_message does its own gating/formatting internally
    # (covered by test_sender.py), keyed off the same identity
    # (session_key) real telegram dispatch checks.
    mock_send.assert_called_once_with(555, "Your balance is $1,000", stats={"cost": 0.01}, session=ANY)
    session_arg = mock_send.call_args.kwargs["session"]
    assert session_arg.stats_key == "user-1"


def test_complete_and_maybe_retry_resumes_session_and_delivers_via_email():
    pending = {
        "session_key": "<thread@synapse.local>", "session_id": "",
        "failed_command": "etrade balance", "retry_channel": "email",
        "email_to": "user@example.com", "email_subject": "Check my balance",
        "email_message_id": "<orig@example.com>", "email_references": "<orig@example.com>",
    }
    fake_result = MagicMock(session_id="sess-new", output="Your balance is $1,000", stats={"cost": 0.01})
    mock_sm = MagicMock()

    with patch("services.ingestion.core.pipe.pipe_to_provider", return_value=fake_result) as mock_pipe, \
         patch("services.ingestion.channels.email.reply.send_reply") as mock_send:
        etrade_pin_auth.complete_and_maybe_retry(pending, mock_sm)

    mock_pipe.assert_called_once_with(ANY, session_id=None)  # empty session_id -> fresh session
    retry_prompt = mock_pipe.call_args.args[0]
    assert "Type: email" in retry_prompt
    assert "Sender: user@example.com" in retry_prompt
    mock_sm.save_session.assert_called_once_with("<thread@synapse.local>", "sess-new", daily_reset=False)
    # The raw stats dict and a UserSession handle are forwarded as-is —
    # send_reply does its own gating/formatting internally (covered by
    # test_reply.py). The handle's stats identity keys off the sender's
    # email address, not the (message-id-based) session_key — mirrors
    # process_email's own check.
    mock_send.assert_called_once_with(
        to_addr="user@example.com", subject="Check my balance", body="Your balance is $1,000",
        original_message_id="<orig@example.com>", original_references="<orig@example.com>",
        stats={"cost": 0.01}, session=ANY,
    )
    session_arg = mock_send.call_args.kwargs["session"]
    assert session_arg.stats_key == "user@example.com"


def test_complete_and_maybe_retry_replays_reminder_task_and_delivers_via_email():
    pending = {"reminder_task": "Run options-bot scan --tickers AAPL,MSFT", "retry_channel": "email", "email_to": "user@example.com"}
    fake_result = MagicMock(session_id="sess-new", output="Found 2 opportunities", stats={"cost": 0.01})
    mock_sm = MagicMock()

    with patch("services.ingestion.core.pipe.pipe_to_provider", return_value=fake_result) as mock_pipe, \
         patch("services.ingestion.channels.email.reply.send_reply") as mock_send:
        etrade_pin_auth.complete_and_maybe_retry(pending, mock_sm)

    mock_pipe.assert_called_once()
    assert mock_pipe.call_args.kwargs["model"] == "work"
    prompt_arg = mock_pipe.call_args.args[0]
    assert "Run options-bot scan --tickers AAPL,MSFT" in prompt_arg
    # The raw stats dict and a UserSession handle are forwarded as-is —
    # send_reply does its own gating internally. Reminders carry no
    # session/user key, so the handle's stats identity falls back to the
    # reminder's own email_to field.
    mock_send.assert_called_once_with(
        to_addr="user@example.com", subject="Synapse: E*TRADE retry result", body="Found 2 opportunities",
        original_message_id="", original_references="",
        stats={"cost": 0.01}, session=ANY,
    )
    session_arg = mock_send.call_args.kwargs["session"]
    assert session_arg.stats_key == "user@example.com"


def test_complete_and_maybe_retry_replays_reminder_task_and_delivers_via_telegram():
    pending = {"reminder_task": "Run options-bot scan --tickers AAPL,MSFT", "retry_channel": "telegram", "chat_id": 555}
    fake_result = MagicMock(session_id="sess-new", output="Found 2 opportunities", stats={"cost": 0.01})
    mock_sm = MagicMock()

    with patch("services.ingestion.core.pipe.pipe_to_provider", return_value=fake_result) as mock_pipe, \
         patch("services.ingestion.channels.telegram.sender.send_telegram_message") as mock_send, \
         patch("services.ingestion.config") as mock_config:
        mock_config.TELEGRAM_ALLOWED_USER_IDS = [999]

        etrade_pin_auth.complete_and_maybe_retry(pending, mock_sm)

    # Reminders carry no session/user key, so the handle's stats identity
    # for the telegram case falls back to the configured allowed user id —
    # the same identity scheduler.py's _handle_work_reminder uses.
    mock_send.assert_called_once_with(555, "Found 2 opportunities", stats={"cost": 0.01}, session=ANY)
    session_arg = mock_send.call_args.kwargs["session"]
    assert session_arg.stats_key == "999"


def test_complete_and_maybe_retry_noop_when_no_retry_fields():
    pending = {"channel": "telegram", "chat_id": 12345, "prompt_message_id": 555}
    mock_sm = MagicMock()

    with patch("services.ingestion.core.pipe.pipe_to_provider") as mock_pipe, \
         patch("services.ingestion.channels.telegram.sender.send_telegram_message") as mock_send, \
         patch("services.ingestion.channels.email.reply.send_reply") as mock_send_email:
        etrade_pin_auth.complete_and_maybe_retry(pending, mock_sm)

    mock_pipe.assert_not_called()
    mock_send.assert_not_called()
    mock_send_email.assert_not_called()
