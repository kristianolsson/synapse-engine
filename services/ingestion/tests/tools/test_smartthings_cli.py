"""Tests for the smartthings CLI's auth subcommand: manual code-paste
OAuth flow and the exchange/save/error-handling around it."""

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
    monkeypatch.setattr("builtins.input", lambda prompt="": "the-code")
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: None)

    saved = {}
    monkeypatch.setattr(auth, "exchange_code", lambda cid, secret, redirect_uri, code: {
        "access_token": "AT", "refresh_token": "RT", "expires_in": 86400
    })
    monkeypatch.setattr(auth, "save_token", lambda token_path, token_response: saved.update(token_response))

    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
    args = argparse.Namespace(redirect_uri=None)

    smartthings_cli.cmd_auth(args, env)
    assert saved["access_token"] == "AT"


def test_cmd_auth_uses_default_redirect_uri_when_not_overridden(monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda prompt="": "the-code")

    opened_urls = []
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: opened_urls.append(url))

    exchanged = {}

    def fake_exchange_code(cid, secret, redirect_uri, code):
        exchanged["redirect_uri"] = redirect_uri
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 86400}

    monkeypatch.setattr(auth, "exchange_code", fake_exchange_code)
    monkeypatch.setattr(auth, "save_token", lambda token_path, token_response: None)

    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
    args = argparse.Namespace(redirect_uri=None)

    smartthings_cli.cmd_auth(args, env)

    from urllib.parse import quote
    assert f"redirect_uri={quote(auth.DEFAULT_REDIRECT_URI, safe='')}" in opened_urls[0]
    assert exchanged["redirect_uri"] == auth.DEFAULT_REDIRECT_URI


def test_cmd_auth_redirect_uri_override_used_for_authorize_and_exchange(monkeypatch, tmp_path):
    """A custom --redirect-uri must flow into both the authorize URL and
    the code exchange — e.g. a self-hosted HTTPS page instead of httpbin."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "the-code")

    opened_urls = []
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: opened_urls.append(url))

    exchanged = {}

    def fake_exchange_code(cid, secret, redirect_uri, code):
        exchanged["redirect_uri"] = redirect_uri
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 86400}

    monkeypatch.setattr(auth, "exchange_code", fake_exchange_code)
    monkeypatch.setattr(auth, "save_token", lambda token_path, token_response: None)

    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
    args = argparse.Namespace(redirect_uri="https://example.com/callback")

    smartthings_cli.cmd_auth(args, env)

    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcallback" in opened_urls[0]
    assert exchanged["redirect_uri"] == "https://example.com/callback"


def test_cmd_auth_no_code_entered_fails_loud(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: None)

    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
    args = argparse.Namespace(redirect_uri=None)

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_auth(args, env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"


def test_cmd_auth_exchange_failure_fails_loud(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "bad-code")
    monkeypatch.setattr(smartthings_cli.webbrowser, "open", lambda url: None)

    def raise_auth_error(cid, secret, redirect_uri, code):
        raise auth.SmartThingsAuthError("Code exchange failed (400): invalid_grant")

    monkeypatch.setattr(auth, "exchange_code", raise_auth_error)

    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
    args = argparse.Namespace(redirect_uri=None)

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_auth(args, env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"
    assert "invalid_grant" in out["error"]


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
    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }

    smartthings_cli.cmd_list_devices(argparse.Namespace(), env)

    out = json.loads(capsys.readouterr().out)
    assert out["devices"] == [{"id": "d1", "label": "Kitchen Light"}]


def test_cmd_get_status_resolves_device_and_returns_status(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(
        devices=[{"deviceId": "d1", "label": "Kitchen Light"}],
        status={"switch": {"switch": {"value": "on"}}},
    )
    _patch_client_and_token(monkeypatch, fake_client)
    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _resolver_stub(fake_client, tmp_path))

    smartthings_cli.cmd_get_status(argparse.Namespace(device="kitchen"), env)

    out = json.loads(capsys.readouterr().out)
    assert out["device"]["id"] == "d1"
    assert out["status"]["switch"]["switch"]["value"] == "on"


def test_cmd_get_status_not_found_fails_loud(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(devices=[{"deviceId": "d1", "label": "Kitchen Light"}])
    _patch_client_and_token(monkeypatch, fake_client)
    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
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
    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
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
    env = {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }
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


# ── I2/I3: API/auth errors must be reported via the CLI's own error
# handlers (auth_failed/api_error), never fall through to the generic
# "Unexpected error" catch-all in main() ──


def _make_env(tmp_path):
    return {
        "client_id": "cid",
        "client_secret": "secret",
        "token_path": tmp_path / "token.json",
        "device_cache_path": tmp_path / "cache.json",
        "device_cache_ttl": 300,
    }


def test_cmd_list_devices_reports_api_error_on_client_failure(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(raise_on="list_devices")
    _patch_client_and_token(monkeypatch, fake_client)
    env = _make_env(tmp_path)

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_list_devices(argparse.Namespace(), env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "api_error"


def test_cmd_get_status_reports_api_error_on_client_failure(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(
        devices=[{"deviceId": "d1", "label": "Kitchen Light"}],
        raise_on="get_device_status",
    )
    _patch_client_and_token(monkeypatch, fake_client)
    env = _make_env(tmp_path)
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _resolver_stub(fake_client, tmp_path))

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_get_status(argparse.Namespace(device="kitchen"), env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "api_error"


def test_cmd_set_state_reports_api_error_on_client_failure(monkeypatch, capsys, tmp_path):
    fake_client = _FakeClient(
        devices=[{"deviceId": "d1", "label": "Kitchen Light"}],
        raise_on="send_commands",
    )
    _patch_client_and_token(monkeypatch, fake_client)
    env = _make_env(tmp_path)
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _resolver_stub(fake_client, tmp_path))

    with pytest.raises(SystemExit):
        smartthings_cli.cmd_set_state(
            argparse.Namespace(device="kitchen", capability="switch", command="on", args=[]),
            env,
        )
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "api_error"


def test_resolve_one_reports_api_error_when_resolver_raises(monkeypatch, capsys, tmp_path):
    """A SmartThingsAPIError raised during device resolution (e.g. a 429
    on the live device-list refresh) must be reported as api_error, not
    escape to the generic catch-all in main()."""
    class _RaisingResolver:
        def resolve(self, name):
            raise SmartThingsAPIError("rate limited")

    env = _make_env(tmp_path)
    monkeypatch.setattr(smartthings_cli, "_get_resolver", lambda client, env: _RaisingResolver())

    with pytest.raises(SystemExit):
        smartthings_cli._resolve_one("kitchen", client=None, env=env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "api_error"
    assert "rate limited" in out["error"]


def test_get_client_reports_auth_failed_when_token_refresh_raises(monkeypatch, capsys, tmp_path):
    env = _make_env(tmp_path)
    monkeypatch.setattr(
        smartthings_cli.auth, "get_valid_access_token",
        lambda *a, **kw: (_ for _ in ()).throw(auth.SmartThingsAuthError("token refresh failed")),
    )

    with pytest.raises(SystemExit):
        smartthings_cli._get_client(env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"


def test_get_client_reports_auth_failed_on_corrupted_token_file(capsys, tmp_path):
    """A truncated/corrupted token file makes auth.load_token's json.load
    raise a bare JSONDecodeError — _get_client must catch that too, not
    just SmartThingsAuthError, or it falls through to the generic
    "Unexpected error" catch-all."""
    token_path = tmp_path / "token.json"
    token_path.write_text("{not valid json")
    env = _make_env(tmp_path)
    env["token_path"] = token_path

    with pytest.raises(SystemExit):
        smartthings_cli._get_client(env)
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "auth_failed"
    assert "corrupted" in out["error"].lower()
