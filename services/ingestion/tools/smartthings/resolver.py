"""Resolve a user-facing device name (e.g. "kitchen light") to a
SmartThings device id, backed by a short-TTL local cache of
list-devices output so repeated resolutions don't burn API calls."""

import difflib
import json
import os
import time
from pathlib import Path
from typing import Optional

FUZZY_MATCH_THRESHOLD = 0.6


class DeviceResolver:
    def __init__(self, client, cache_path, ttl_seconds: int):
        self._client = client
        self._cache_path = Path(cache_path)
        self._ttl_seconds = ttl_seconds

    def _read_cache(self) -> Optional[list]:
        if not self._cache_path.exists():
            return None
        with open(self._cache_path) as f:
            cache = json.load(f)
        if time.time() - cache["fetched_at"] > self._ttl_seconds:
            return None
        return cache["devices"]

    def _write_cache(self, devices: list) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._cache_path.with_suffix(".json.tmp")
        payload = {"fetched_at": time.time(), "devices": devices}
        with open(tmp_path, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._cache_path)

    def _devices(self, force_refresh: bool = False) -> list:
        if not force_refresh:
            cached = self._read_cache()
            if cached is not None:
                return cached
        raw = self._client.list_devices()
        devices = [{"id": d["deviceId"], "label": d.get("label") or d.get("name", "")} for d in raw]
        self._write_cache(devices)
        return devices

    @staticmethod
    def _matches(query: str, label: str) -> bool:
        query = query.strip().lower()
        label = label.lower()
        if query in label:
            return True
        return difflib.SequenceMatcher(None, query, label).ratio() >= FUZZY_MATCH_THRESHOLD

    def resolve(self, name: str) -> list:
        """Return every device matching *name* — by exact device id
        first, else fuzzy label match. Empty means no match (caller
        reports not_found); more than one means ambiguous (caller must
        ask the user to disambiguate)."""
        devices = self._devices()

        exact_id = [d for d in devices if d["id"] == name]
        if exact_id:
            return exact_id

        matches = [d for d in devices if self._matches(name, d["label"])]
        if not matches:
            # Cache may be stale (e.g. a newly added device) — refresh once and retry.
            devices = self._devices(force_refresh=True)
            exact_id = [d for d in devices if d["id"] == name]
            if exact_id:
                return exact_id
            matches = [d for d in devices if self._matches(name, d["label"])]
        return matches
