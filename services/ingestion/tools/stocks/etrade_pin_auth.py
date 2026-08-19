"""Manual OAuth PIN fallback for E*TRADE authentication.

Automated Playwright-driven login gets blocked by E*TRADE's bot detection
(Akamai Bot Manager + RSA/Cyota device fingerprinting reject the automated
form submission regardless of typing speed or mouse movement — confirmed
by direct testing). The only reliable path is a human completing the
OAuth authorize flow themselves in a real, unautomated browser.

This implements the classic three-legged OAuth PIN flow (the same one
services/ingestion/tools/stocks/auth.py's ETradeAuth uses interactively),
split into two steps so the verification code can round-trip through a
messaging channel (Telegram/email) instead of a blocking terminal input(),
and so it survives across process boundaries: a scheduled job's
short-lived subprocess sends the prompt (start_pin_auth), and the
long-lived Telegram/email listener process completes the exchange
(finish_pin_auth) whenever the reply arrives.
"""

import json
import time
from pathlib import Path
from typing import Optional

from authlib.integrations.requests_client import OAuth1Session

PENDING_FILE = Path.home() / ".etrade_pending_auth.json"
PENDING_TTL_SECONDS = 30 * 60
TOKEN_FILE = Path.home() / ".etrade_tokens"


def load_pending() -> Optional[dict]:
    """Return the pending auth request, or None if there isn't one or it expired."""
    if not PENDING_FILE.exists():
        return None
    try:
        data = json.loads(PENDING_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("created_at", 0) > PENDING_TTL_SECONDS:
        clear_pending()
        return None
    return data


def clear_pending() -> None:
    PENDING_FILE.unlink(missing_ok=True)


def mark_prompt_sent(**fields) -> None:
    """Merge channel-correlation fields (channel, chat_id, prompt_message_id,
    reply_subject, etc.) into the pending request after the prompt has
    actually been delivered. No-op if there's no pending request (e.g. it
    expired between start_pin_auth and the send)."""
    pending = load_pending()
    if pending is None:
        return
    pending.update(fields)
    PENDING_FILE.write_text(json.dumps(pending))


def backfill_session_id(session_key: str, session_id: str) -> None:
    """If a pending E*TRADE auth request is waiting on this exact
    session_key but didn't yet know its session_id (it was the first
    message in a brand-new thread when the auth fallback fired), fill
    it in now that the provider has assigned one. No-op otherwise."""
    if not session_id:
        return
    pending = load_pending()
    if pending and pending.get("session_key") == session_key and not pending.get("session_id"):
        mark_prompt_sent(session_id=session_id)


def start_pin_auth(consumer_key: str, consumer_secret: str) -> dict:
    """Fetch a request token + authorize URL from E*TRADE and persist
    pending state for finish_pin_auth to complete later, possibly from a
    different process. Call mark_prompt_sent() once the prompt has
    actually been delivered, to record which channel/message to match
    the reply against.

    Returns the pending dict, including 'authorize_url' to send to the user.
    """
    client = OAuth1Session(client_id=consumer_key, client_secret=consumer_secret, redirect_uri="oob")
    request_token = client.fetch_request_token(
        url="https://api.etrade.com/oauth/request_token", params={"format": "json"}
    )
    authorize_url = (
        f"https://us.etrade.com/e/t/etws/authorize?key={consumer_key}&token={request_token['oauth_token']}"
    )
    pending = {
        "oauth_token": request_token["oauth_token"],
        "oauth_token_secret": request_token["oauth_token_secret"],
        "authorize_url": authorize_url,
        "created_at": time.time(),
    }
    PENDING_FILE.write_text(json.dumps(pending))
    PENDING_FILE.chmod(0o600)
    return pending


def finish_pin_auth(pending: dict, verifier: str, consumer_key: str, consumer_secret: str) -> dict:
    """Exchange the pasted verification code for a real access token.

    Does not clear the pending file — the caller clears it only after
    also saving the resulting token, so a crash in between doesn't lose
    the in-flight request-token state.
    """
    client = OAuth1Session(
        client_id=consumer_key,
        client_secret=consumer_secret,
        token=pending["oauth_token"],
        token_secret=pending["oauth_token_secret"],
        redirect_uri="oob",
    )
    client.fetch_access_token(url="https://api.etrade.com/oauth/access_token", verifier=verifier.strip())
    return {
        "access_token": client.token.get("oauth_token"),
        "access_token_secret": client.token.get("oauth_token_secret"),
    }


def save_access_token(access_token: str, access_token_secret: str, sandbox: bool = False) -> None:
    """Save the exchanged token to the same file/schema
    ETradeAuth/WetradeAuth read from on their next run, so the normal
    login flow picks it up transparently."""
    data = {"access_token": access_token, "access_token_secret": access_token_secret, "sandbox": sandbox}
    TOKEN_FILE.write_text(json.dumps(data))
    TOKEN_FILE.chmod(0o600)
