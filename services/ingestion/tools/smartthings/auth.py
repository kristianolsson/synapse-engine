"""SmartThings OAuth2 authorization-code flow with silently-rotating
refresh tokens. See vault/projects/coding/smartthings-integration.md for
the full design rationale (why a static PAT doesn't work here, why no
scheduler safety-net, etc)."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests


def load_token(token_path) -> Optional[dict]:
    token_path = Path(token_path)
    if not token_path.exists():
        return None
    with open(token_path) as f:
        return json.load(f)


def save_token(token_path, token_response: dict) -> None:
    """Atomically persist access_token + refresh_token + computed expiry.
    The rotated refresh_token must be saved immediately on every refresh —
    SmartThings invalidates the old one as soon as a new one is issued."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=token_response["expires_in"])
    ).isoformat()
    data = {
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "expires_at": expires_at,
    }
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = token_path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, token_path)


AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
TOKEN_URL = "https://api.smartthings.com/oauth/token"
SCOPES = "r:devices:* x:devices:*"

# Refresh this long before actual expiry so a request never races a
# token that expires mid-flight.
EXPIRY_BUFFER_SECONDS = 60


class SmartThingsAuthError(Exception):
    """Raised when no token exists yet, or a refresh is rejected
    (revoked/invalid) — always fatal, the caller must run
    `smartthings auth` again. No silent retries."""


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    """Exchange a one-time authorization code for the first access/refresh token pair."""
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        auth=(client_id, client_secret),
        timeout=15,
    )
    if resp.status_code != 200:
        raise SmartThingsAuthError(f"Code exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
        timeout=15,
    )
    if resp.status_code != 200:
        raise SmartThingsAuthError(f"Token refresh failed ({resp.status_code}): {resp.text}")
    return resp.json()


def get_valid_access_token(token_path, client_id: str, client_secret: str) -> str:
    """Return a live access_token, refreshing first if it's stale.
    Raises SmartThingsAuthError if no token exists yet, or if the
    refresh is rejected — never retries silently."""
    token = load_token(token_path)
    if token is None:
        raise SmartThingsAuthError(
            "No SmartThings token found — run 'smartthings auth' to authorize."
        )

    expires_at = datetime.fromisoformat(token["expires_at"])
    if datetime.now(timezone.utc) < expires_at - timedelta(seconds=EXPIRY_BUFFER_SECONDS):
        return token["access_token"]

    try:
        refreshed = _refresh(client_id, client_secret, token["refresh_token"])
    except SmartThingsAuthError:
        raise SmartThingsAuthError(
            "SmartThings auth expired — run 'smartthings auth' to reauthorize"
        )
    save_token(token_path, refreshed)
    return refreshed["access_token"]
