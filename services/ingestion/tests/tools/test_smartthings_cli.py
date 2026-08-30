"""Tests for the smartthings CLI's auth subcommand: OAuth callback
capture and the exchange/save/error-handling around it."""

import argparse
import json

import pytest

from services.ingestion.tools import smartthings_cli
from services.ingestion.tools.smartthings import auth


def test_err_prints_json_and_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        smartthings_cli._err("something broke", "api_error")
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {"error": "something broke", "code": "api_error"}


def test_out_prints_json(capsys):
    smartthings_cli._out({"ok": True})
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True}


def test_cmd_auth_happy_path_exchanges_and_saves_token(monkeypatch, tmp_path):
    monkeypatch.setattr(smartthings_cli, "_capture_authorization_code", lambda port: ("the-code", "expected-state"))
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(smartthings_cli.secrets, "token_urlsafe", lambda n: "expected-state")

    saved = {}
    monkeypatch.setattr(auth, "exchange_code", lambda cid, secret, redirect_uri, code: {
        "access_token": "AT", "refresh_token": "RT", "expires_in": 86400
    })
    monkeypatch.setattr(auth, "save_token", lambda token_path, token_response: saved.update(token_response))

    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}
    args = argparse.Namespace(port=8765)

    smartthings_cli.cmd_auth(args, env)
    assert saved["access_token"] == "AT"


def test_cmd_auth_state_mismatch_fails_loud(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(smartthings_cli, "_capture_authorization_code", lambda port: ("the-code", "wrong-state"))
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(smartthings_cli.secrets, "token_urlsafe", lambda n: "expected-state")

    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}
    args = argparse.Namespace(port=8765)

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_auth(args, env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"
    assert "state mismatch" in out["error"].lower()


def test_cmd_auth_no_code_received_fails_loud(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(smartthings_cli, "_capture_authorization_code", lambda port: (None, "expected-state"))
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(smartthings_cli.secrets, "token_urlsafe", lambda n: "expected-state")

    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}
    args = argparse.Namespace(port=8765)

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_auth(args, env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"


def test_capture_authorization_code_reads_query_params_from_real_request():
    import threading
    import time
    import requests as real_requests

    port = 18765
    result = {}

    def run_capture():
        result["code"], result["state"] = smartthings_cli._capture_authorization_code(port)

    thread = threading.Thread(target=run_capture)
    thread.start()
    time.sleep(0.2)  # let the server start listening before we hit it
    real_requests.get(f"http://localhost:{port}/callback?code=abc123&state=xyz789", timeout=5)
    thread.join(timeout=5)

    assert result["code"] == "abc123"
    assert result["state"] == "xyz789"
