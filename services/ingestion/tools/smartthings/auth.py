"""SmartThings OAuth2 authorization-code flow with silently-rotating
refresh tokens. See vault/projects/coding/smartthings-integration.md for
the full design rationale (why a static PAT doesn't work here, why no
scheduler safety-net, etc)."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


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
