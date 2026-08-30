"""Tests for the SmartThings REST client's request/backoff core."""

import pytest

from services.ingestion.tools.smartthings.client import SmartThingsClient, SmartThingsAPIError


class _FakeResponse:
    def __init__(self, status_code, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json_body = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.content = json_body is not None
        self.text = text or str(self._json_body)

    def json(self):
        return self._json_body


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, json))
        return self._responses.pop(0)


def test_request_returns_json_on_success():
    session = _FakeSession([_FakeResponse(200, {"items": []})])
    client = SmartThingsClient("token", session=session)
    result = client._request("GET", "/devices")
    assert result == {"items": []}
    assert session.calls[0][0] == "GET"
    assert session.calls[0][1] == "https://api.smartthings.com/v1/devices"


def test_request_sends_bearer_token():
    session = _FakeSession([_FakeResponse(200, {})])
    client = SmartThingsClient("secret-token", session=session)
    client._request("GET", "/devices")
    # headers aren't captured by _FakeSession.calls; verify via a session that records them
    class _RecordingSession(_FakeSession):
        def request(self, method, url, headers=None, json=None, timeout=None):
            self.last_headers = headers
            return super().request(method, url, headers=headers, json=json, timeout=timeout)

    recording = _RecordingSession([_FakeResponse(200, {})])
    SmartThingsClient("secret-token", session=recording)._request("GET", "/devices")
    assert recording.last_headers["Authorization"] == "Bearer secret-token"


def test_request_backs_off_once_on_429_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    session = _FakeSession([
        _FakeResponse(429, headers={"X-RateLimit-Reset": "2"}),
        _FakeResponse(200, {"ok": True}),
    ])
    client = SmartThingsClient("token", session=session)
    result = client._request("GET", "/devices")

    assert result == {"ok": True}
    assert sleeps == [2.0]
    assert len(session.calls) == 2


def test_request_fails_loud_after_one_retry_still_rate_limited(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    session = _FakeSession([
        _FakeResponse(429, headers={"X-RateLimit-Reset": "1"}),
        _FakeResponse(429, headers={"X-RateLimit-Reset": "1"}),
    ])
    client = SmartThingsClient("token", session=session)

    with pytest.raises(SmartThingsAPIError, match="rate limit"):
        client._request("GET", "/devices")
    assert len(session.calls) == 2  # initial + exactly one retry, no infinite loop


def test_request_raises_on_non_2xx_non_429():
    session = _FakeSession([_FakeResponse(500, text="server error")])
    client = SmartThingsClient("token", session=session)
    with pytest.raises(SmartThingsAPIError, match="500"):
        client._request("GET", "/devices")


def test_request_handles_malformed_rate_limit_reset_header(monkeypatch):
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    session = _FakeSession([
        _FakeResponse(429, headers={"X-RateLimit-Reset": "not-a-number"}),
        _FakeResponse(200, {"ok": True}),
    ])
    client = SmartThingsClient("token", session=session)
    result = client._request("GET", "/devices")

    assert result == {"ok": True}
    assert sleeps == [1.0]  # falls back to 1.0 when parsing fails
    assert len(session.calls) == 2


def test_request_returns_empty_dict_on_2xx_with_no_content():
    session = _FakeSession([_FakeResponse(200, json_body=None)])
    client = SmartThingsClient("token", session=session)
    result = client._request("GET", "/devices")
    assert result == {}


def test_list_devices_returns_items():
    session = _FakeSession([_FakeResponse(200, {"items": [{"deviceId": "d1", "label": "Kitchen Light"}]})])
    client = SmartThingsClient("token", session=session)
    devices = client.list_devices()
    assert devices == [{"deviceId": "d1", "label": "Kitchen Light"}]
    assert session.calls[0] == ("GET", "https://api.smartthings.com/v1/devices", None)


def test_get_device_status_hits_correct_path():
    session = _FakeSession([_FakeResponse(200, {"switch": {"switch": {"value": "on"}}})])
    client = SmartThingsClient("token", session=session)
    status = client.get_device_status("d1")
    assert status == {"switch": {"switch": {"value": "on"}}}
    assert session.calls[0][1] == "https://api.smartthings.com/v1/devices/d1/status"


def test_send_commands_single_chunk_for_small_command_list():
    session = _FakeSession([_FakeResponse(200, {"results": [{"status": "ACCEPTED"}]})])
    client = SmartThingsClient("token", session=session)
    result = client.send_commands("d1", [{"capability": "switch", "command": "on"}])

    assert len(session.calls) == 1
    assert session.calls[0][1] == "https://api.smartthings.com/v1/devices/d1/commands"
    assert session.calls[0][2] == {"commands": [{"capability": "switch", "command": "on"}]}
    assert result["results"] == [{"status": "ACCEPTED"}]


def test_send_commands_splits_into_batches_of_ten():
    commands = [{"capability": "switch", "command": "on"}] * 25
    session = _FakeSession([_FakeResponse(200, {"results": []}) for _ in range(3)])
    client = SmartThingsClient("token", session=session)
    client.send_commands("d1", commands)

    assert len(session.calls) == 3  # 10 + 10 + 5
    assert len(session.calls[0][2]["commands"]) == 10
    assert len(session.calls[1][2]["commands"]) == 10
    assert len(session.calls[2][2]["commands"]) == 5
