#!/usr/bin/env python3
"""SmartThings CLI — query/control SmartThings devices for Synapse.

This CLI is a reusable tool for the Synapse ecosystem. All output is JSON to stdout.
On error, outputs {"error": "<message>", "code": "<error_type>"} and exits non-zero.

Error codes:
  auth_failed   — OAuth authorization or token exchange failed
  config_error  — Missing or invalid configuration
  api_error     — SmartThings API returned an unexpected response
  not_found     — No device matched the given name/id
  ambiguous     — Multiple devices matched the given name

Usage:
  smartthings auth [--port 8765]
  smartthings list-devices
  smartthings get-status <device>
  smartthings set-state <device> <capability> <command> [args...]
"""

import argparse
import json
import os
import secrets
import sys
import webbrowser
from pathlib import Path
from typing import Optional

# Resolve the services.* package path, matching every other tool CLI's bootstrap.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from services.ingestion.tools.smartthings import auth
from services.ingestion.tools.smartthings.client import SmartThingsClient, SmartThingsAPIError
from services.ingestion.tools.smartthings.resolver import DeviceResolver


# ── Helpers ──────────────────────────────────────────────────────────────────

def _err(message: str, code: str) -> None:
    print(json.dumps({"error": message, "code": code}, ensure_ascii=False))
    sys.exit(1)


def _out(data) -> None:
    print(json.dumps(data, ensure_ascii=False, default=str))


def _load_env() -> dict:
    from services.ingestion import config as syn_config
    return {
        "client_id": os.getenv("SMARTTHINGS_CLIENT_ID", "") or syn_config.SMARTTHINGS_CLIENT_ID,
        "client_secret": os.getenv("SMARTTHINGS_CLIENT_SECRET", "") or syn_config.SMARTTHINGS_CLIENT_SECRET,
        "token_path": Path(syn_config.SMARTTHINGS_TOKEN_PATH),
        "device_cache_path": Path(syn_config.SMARTTHINGS_DEVICE_CACHE_PATH),
        "device_cache_ttl": syn_config.SMARTTHINGS_DEVICE_CACHE_TTL_SECONDS,
    }


def _capture_authorization_code(port: int) -> tuple:
    """Blocks until a browser redirect to /callback hits the local callback
    server, then returns (code, state) from its query string. Ignores stray
    requests to other paths (e.g., /favicon.ico) and loops until the matching
    callback is received, up to a cap of 20 stray requests."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    captured = {}
    stray_count = 0
    MAX_STRAYS = 20

    class _CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # suppress default request logging to stderr

        def do_GET(self):
            nonlocal stray_count
            path = urlparse(self.path).path
            if path == "/callback":
                params = parse_qs(urlparse(self.path).query)
                captured["code"] = params.get("code", [None])[0]
                captured["state"] = params.get("state", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"SmartThings authorization complete. You can close this tab.")
            else:
                # Stray request (e.g., /favicon.ico), respond with 404 and loop
                stray_count += 1
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Not found.")

    server = HTTPServer(("localhost", port), _CallbackHandler)
    # Loop until we capture a real callback or hit the stray cap
    while not captured and stray_count < MAX_STRAYS:
        server.handle_request()  # blocks for exactly one request, then returns
    return captured.get("code"), captured.get("state")


# ── Subcommands ───────────────────────────────────────────────────────────────

def cmd_auth(args, env: dict) -> None:
    """One-time interactive OAuth flow: opens a browser, runs a local
    callback server to capture the authorization code, exchanges it for
    the first access/refresh token pair."""
    if not env["client_id"] or not env["client_secret"]:
        _err("SMARTTHINGS_CLIENT_ID and SMARTTHINGS_CLIENT_SECRET must be set in .env", "config_error")

    redirect_uri = f"http://localhost:{args.port}/callback"
    state = secrets.token_urlsafe(16)
    authorize_url = auth.build_authorize_url(env["client_id"], redirect_uri, state)

    print(f"Opening browser for SmartThings authorization:\n{authorize_url}", file=sys.stderr)
    webbrowser.open(authorize_url)

    code, returned_state = _capture_authorization_code(args.port)

    if returned_state != state:
        _err("OAuth state mismatch — possible interference, aborting.", "auth_failed")
    if not code:
        _err("No authorization code received from SmartThings.", "auth_failed")

    try:
        token_response = auth.exchange_code(env["client_id"], env["client_secret"], redirect_uri, code)
    except auth.SmartThingsAuthError as e:
        _err(str(e), "auth_failed")

    auth.save_token(env["token_path"], token_response)
    _out({"status": "authorized", "token_path": str(env["token_path"])})


def _get_client(env: dict) -> SmartThingsClient:
    try:
        token = auth.get_valid_access_token(env["token_path"], env["client_id"], env["client_secret"])
    except auth.SmartThingsAuthError as e:
        _err(str(e), "auth_failed")
    except (json.JSONDecodeError, KeyError) as e:
        _err(f"SmartThings token file is corrupted or invalid: {e}", "auth_failed")
    return SmartThingsClient(token)


def _get_resolver(client: SmartThingsClient, env: dict) -> DeviceResolver:
    return DeviceResolver(client, env["device_cache_path"], env["device_cache_ttl"])


def _resolve_one(device_name: str, client: SmartThingsClient, env: dict) -> dict:
    resolver = _get_resolver(client, env)
    try:
        matches = resolver.resolve(device_name)
    except SmartThingsAPIError as e:
        _err(str(e), "api_error")
    if not matches:
        _err(f"No device found matching '{device_name}'", "not_found")
    if len(matches) > 1:
        _err(
            f"Multiple devices match '{device_name}': " + ", ".join(m["label"] for m in matches),
            "ambiguous",
        )
    return matches[0]


def _coerce(value: str):
    """Best-effort turn a CLI string arg into int/float/bool for the
    SmartThings commands payload (e.g. `setLevel 50` needs the int 50,
    not the string "50")."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def cmd_list_devices(args, env: dict) -> None:
    client = _get_client(env)
    try:
        devices = client.list_devices()
    except SmartThingsAPIError as e:
        _err(str(e), "api_error")
    _out({"devices": [{"id": d["deviceId"], "label": d.get("label") or d.get("name", "")} for d in devices]})


def cmd_get_status(args, env: dict) -> None:
    client = _get_client(env)
    device = _resolve_one(args.device, client, env)
    try:
        status = client.get_device_status(device["id"])
    except SmartThingsAPIError as e:
        _err(str(e), "api_error")
    _out({"device": device, "status": status})


def cmd_set_state(args, env: dict) -> None:
    client = _get_client(env)
    device = _resolve_one(args.device, client, env)
    arguments = [_coerce(a) for a in args.args]
    command = {"capability": args.capability, "command": args.command, "arguments": arguments}
    try:
        result = client.send_commands(device["id"], [command])
    except SmartThingsAPIError as e:
        _err(str(e), "api_error")
    _out({"device": device, "result": result})


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="smartthings", description="SmartThings CLI for Synapse.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="One-time interactive OAuth authorization")
    p_auth.add_argument(
        "--port", type=int, default=8765,
        help="Local callback port (must match the SmartApp's registered redirect URI)",
    )

    sub.add_parser("list-devices", help="List all SmartThings devices")

    p_status = sub.add_parser("get-status", help="Get a device's current status")
    p_status.add_argument("device", help="Device name (fuzzy-matched) or exact SmartThings device id")

    p_set = sub.add_parser("set-state", help="Send a command to a device")
    p_set.add_argument("device", help="Device name (fuzzy-matched) or exact SmartThings device id")
    p_set.add_argument("capability", help="e.g. switch, switchLevel, thermostatMode")
    p_set.add_argument("command", help="e.g. on, off, setLevel")
    p_set.add_argument("args", nargs="*", help="Command arguments, e.g. 50 for setLevel")

    args = parser.parse_args()
    env = _load_env()

    dispatch = {
        "auth": cmd_auth,
        "list-devices": cmd_list_devices,
        "get-status": cmd_get_status,
        "set-state": cmd_set_state,
    }
    try:
        dispatch[args.command](args, env)
    except SystemExit:
        raise
    except Exception as e:
        _err(f"Unexpected error: {e}", "api_error")


if __name__ == "__main__":
    main()
