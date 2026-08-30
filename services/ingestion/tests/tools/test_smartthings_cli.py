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


def test_capture_authorization_code_ignores_stray_requests_and_waits_for_real_callback():
    import threading
    import time
    import requests as real_requests

    port = 18766
    result = {}

    def run_capture():
        result["code"], result["state"] = smartthings_cli._capture_authorization_code(port)

    thread = threading.Thread(target=run_capture)
    thread.start()
    time.sleep(0.2)  # let the server start listening before we hit it

    # Send a stray request to /favicon.ico (should be ignored)
    try:
        real_requests.get(f"http://localhost:{port}/favicon.ico", timeout=5)
    except Exception:
        pass  # ignore failures from favicon request

    # Now send the real callback request
    real_requests.get(f"http://localhost:{port}/callback?code=xyz456&state=abc789", timeout=5)
    thread.join(timeout=5)

    # Verify the real callback was captured, not the stray request
    assert result["code"] == "xyz456"
    assert result["state"] == "abc789"


# ── Tests for list-devices, get-status, set-state, and helpers ──

from services.ingestion.tools.smartthings.client import SmartThingsAPIError


class _FakeClient:
    def __init__(self, devices=None, status=None, send_result=None, raise_on=None):
        self._devices = devices or []
        self._status = status or {}
        self._send_result = send_result or {"results": []}
        self._raise_on = raise_on  # e.g. "list_devices" to simulate an API error

    def list_devices(self):
        if self._raise_on == "list_devices":
            raise SmartThingsAPIError("boom")
        return self._devices

    def get_device_status(self, device_id):
        if self._raise_on == "get_device_status":
            raise SmartThingsAPIError("boom")
        return self._status

    def send_commands(self, device_id, commands):
        if self._raise_on == "send_commands":
            raise SmartThingsAPIError("boom")
        return self._send_result


def _patch_client_and_token(monkeypatch, fake_client):
    # Patch the name as bound in smartthings_cli's own namespace (it did
    # `from ...client import SmartThingsClient`) — patching the source
    # module's attribute instead would miss, since that import already
    # copied the reference. Matches test_etrade_cli.py's convention of
    # patching etrade_cli.ETradeAuth directly, not stocks.auth.ETradeAuth.
    monkeypatch.setattr(smartthings_cli.auth, "get_valid_access_token", lambda *a, **kw: "AT")
    monkeypatch.setattr(smartthings_cli, "SmartThingsClient", lambda access_token: fake_client)


def test_cmd_list_devices_outputs_id_and_label(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(devices=[{"deviceId": "d1", "label": "Kitchen Light"}])
    _patch_client_and_token(monkeypatch, fake_client)
    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}

    smartthings_cli.cmd_list_devices(argparse.Namespace(), env)

    out = json.loads(capsys.readouterr().out)
    assert out["devices"] == [{"id": "d1", "label": "Kitchen Light"}]


def test_cmd_get_status_resolves_device_and_returns_status(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(
        devices=[{"deviceId": "d1", "label": "Kitchen Light"}],
        status={"switch": {"switch": {"value": "on"}}},
    )
    _patch_client_and_token(monkeypatch, fake_client)
    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _resolver_stub(fake_client, tmp_path))

    smartthings_cli.cmd_get_status(argparse.Namespace(device="kitchen"), env)

    out = json.loads(capsys.readouterr().out)
    assert out["device"]["id"] == "d1"
    assert out["status"]["switch"]["switch"]["value"] == "on"


def test_cmd_get_status_not_found_fails_loud(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(devices=[{"deviceId": "d1", "label": "Kitchen Light"}])
    _patch_client_and_token(monkeypatch, fake_client)
    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _resolver_stub(fake_client, tmp_path))

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_get_status(argparse.Namespace(device="thermostat"), env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "not_found"


def test_cmd_get_status_ambiguous_fails_loud_with_candidates(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(devices=[
        {"deviceId": "d1", "label": "Kitchen Light"},
        {"deviceId": "d2", "label": "Bedroom Light"},
    ])
    _patch_client_and_token(monkeypatch, fake_client)
    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _resolver_stub(fake_client, tmp_path))

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_get_status(argparse.Namespace(device="light"), env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "ambiguous"
    assert "Kitchen Light" in out["error"] and "Bedroom Light" in out["error"]


def test_cmd_set_state_coerces_numeric_argument(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(
        devices=[{"deviceId": "d1", "label": "Kitchen Light"}],
        send_result={"results": [{"status": "ACCEPTED"}]},
    )
    _patch_client_and_token(monkeypatch, fake_client)
    env = {"client_id": "cid", "client_secret": "secret", "token_path": tmp_path / "token.json"}
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _resolver_stub(fake_client, tmp_path))

    smartthings_cli.cmd_set_state(
        argparse.Namespace(device="kitchen", capability="switchLevel", command="setLevel", args=["50"]),
        env,
    )

    out = json.loads(capsys.readouterr().out)
    assert out["result"]["results"] == [{"status": "ACCEPTED"}]


def test_coerce_converts_int_float_bool_and_leaves_strings():
    assert smartthings_cli._coerce("50") == 50
    assert smartthings_cli._coerce("50.5") == 50.5
    assert smartthings_cli._coerce("true") is True
    assert smartthings_cli._coerce("false") is False
    assert smartthings_cli._coerce("auto") == "auto"


def _resolver_stub(fake_client, tmp_path):
    from services.ingestion.tools.smartthings.resolver import DeviceResolver
    return DeviceResolver(fake_client, tmp_path / "cache.json", ttl_seconds=300)
