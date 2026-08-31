"""Tests for SmartThings device-name resolution: TTL cache + fuzzy match."""

import json
import time

from services.ingestion.tools.smartthings.resolver import DeviceResolver


class _FakeClient:
    def __init__(self, devices):
        self._devices = devices
        self.list_devices_calls = 0

    def list_devices(self):
        self.list_devices_calls += 1
        return self._devices


DEVICES = [
    {"deviceId": "d1", "label": "Kitchen Light"},
    {"deviceId": "d2", "label": "Bedroom Light"},
    {"deviceId": "d3", "label": "Living Room Light"},
    {"deviceId": "d4", "label": "Garage Door"},
]


def test_resolve_exact_label_match(tmp_path):
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, tmp_path / "cache.json", ttl_seconds=300)
    matches = resolver.resolve("Kitchen Light")
    assert [m["id"] for m in matches] == ["d1"]


def test_resolve_partial_name_matches_one_device(tmp_path):
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, tmp_path / "cache.json", ttl_seconds=300)
    matches = resolver.resolve("garage")
    assert [m["id"] for m in matches] == ["d4"]


def test_resolve_ambiguous_name_returns_multiple_matches(tmp_path):
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, tmp_path / "cache.json", ttl_seconds=300)
    matches = resolver.resolve("light")
    assert {m["id"] for m in matches} == {"d1", "d2", "d3"}


def test_resolve_by_exact_device_id_bypasses_fuzzy_match(tmp_path):
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, tmp_path / "cache.json", ttl_seconds=300)
    matches = resolver.resolve("d2")
    assert [m["id"] for m in matches] == ["d2"]


def test_resolve_no_match_returns_empty_list(tmp_path):
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, tmp_path / "cache.json", ttl_seconds=300)
    matches = resolver.resolve("thermostat")
    assert matches == []


def test_resolve_distinguishes_devices_sharing_a_word(tmp_path):
    """"Front light" must not also match other "* Light" devices just
    because they share the suffix word — the differentiating word
    ("front" vs "tv"/"stairs") has to match too."""
    devices = [
        {"deviceId": "d1", "label": "Front Light"},
        {"deviceId": "d2", "label": "Tv Light"},
        {"deviceId": "d3", "label": "Stairs Light"},
    ]
    client = _FakeClient(devices)
    resolver = DeviceResolver(client, tmp_path / "cache.json", ttl_seconds=300)
    matches = resolver.resolve("Front Light")
    assert [m["id"] for m in matches] == ["d1"]


def test_resolve_uses_cache_on_second_call_within_ttl(tmp_path):
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, tmp_path / "cache.json", ttl_seconds=300)
    resolver.resolve("Kitchen Light")
    resolver.resolve("Bedroom Light")
    assert client.list_devices_calls == 1  # second resolve() served from cache


def test_resolve_refetches_when_cache_expired(tmp_path):
    client = _FakeClient(DEVICES)
    cache_path = tmp_path / "cache.json"
    resolver = DeviceResolver(client, cache_path, ttl_seconds=0)
    resolver.resolve("Kitchen Light")
    time.sleep(0.01)
    resolver.resolve("Kitchen Light")
    assert client.list_devices_calls == 2


def test_resolve_force_refreshes_once_on_no_match_against_stale_cache(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "fetched_at": time.time(),
        "devices": [{"id": "d1", "label": "Kitchen Light"}],
    }))
    client = _FakeClient(DEVICES)  # "live" data now includes a device not in the stale cache
    resolver = DeviceResolver(client, cache_path, ttl_seconds=300)

    matches = resolver.resolve("Garage Door")
    assert [m["id"] for m in matches] == ["d4"]
    assert client.list_devices_calls == 1  # one forced refresh after the cache miss


def test_cache_file_written_atomically(tmp_path):
    client = _FakeClient(DEVICES)
    cache_path = tmp_path / "cache.json"
    resolver = DeviceResolver(client, cache_path, ttl_seconds=300)
    resolver.resolve("Kitchen Light")
    assert cache_path.exists()
    assert not cache_path.with_suffix(".json.tmp").exists()


def test_resolve_recovers_from_corrupted_cache_invalid_json(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{ invalid json }")
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, cache_path, ttl_seconds=300)

    matches = resolver.resolve("Kitchen Light")
    assert [m["id"] for m in matches] == ["d1"]
    assert client.list_devices_calls == 1  # recovered by fetching live devices


def test_resolve_recovers_from_corrupted_cache_missing_key(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"fetched_at": time.time()}))  # missing "devices" key
    client = _FakeClient(DEVICES)
    resolver = DeviceResolver(client, cache_path, ttl_seconds=300)

    matches = resolver.resolve("Garage Door")
    assert [m["id"] for m in matches] == ["d4"]
    assert client.list_devices_calls == 1  # recovered by fetching live devices
