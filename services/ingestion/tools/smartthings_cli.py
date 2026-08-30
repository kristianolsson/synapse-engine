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


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="smartthings", description="SmartThings CLI for Synapse.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="One-time interactive OAuth authorization")
    p_auth.add_argument(
        "--port", type=int, default=8765,
        help="Local callback port (must match the SmartApp's registered redirect URI)",
    )

    args = parser.parse_args()
    env = _load_env()

    dispatch = {"auth": cmd_auth}
    try:
        dispatch[args.command](args, env)
    except SystemExit:
        raise
    except Exception as e:
        _err(f"Unexpected error: {e}", "api_error")


if __name__ == "__main__":
    main()
