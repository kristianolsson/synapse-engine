"""Thin REST wrapper over the SmartThings Devices API. Rate limits
(confirmed from SmartThings docs, see the spec): get-status/set-state are
capped at 12 req/min per device; back off on 429 using X-RateLimit-Reset,
retry once, then fail loud — never loop."""

import time
from typing import Optional

import requests

BASE_URL = "https://api.smartthings.com/v1"
MAX_COMMANDS_PER_REQUEST = 10
MAX_RATE_LIMIT_RETRIES = 1


class SmartThingsAPIError(Exception):
    """Any non-2xx response, or a 429 that persists past the single backoff retry."""


class SmartThingsClient:
    def __init__(self, access_token: str, session: Optional[requests.Session] = None):
        self._token = access_token
        self._session = session or requests.Session()

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> dict:
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        attempts = 0
        while True:
            resp = self._session.request(method, url, headers=headers, json=json_body, timeout=15)
            if resp.status_code == 429:
                if attempts >= MAX_RATE_LIMIT_RETRIES:
                    raise SmartThingsAPIError(
                        f"SmartThings rate limit exceeded for {method} {path} after backoff retry"
                    )
                reset_seconds = float(resp.headers.get("X-RateLimit-Reset", "1"))
                time.sleep(max(reset_seconds, 0))
                attempts += 1
                continue
            if not resp.ok:
                raise SmartThingsAPIError(
                    f"SmartThings API error {resp.status_code} for {method} {path}: {resp.text}"
                )
            if not resp.content:
                return {}
            return resp.json()
