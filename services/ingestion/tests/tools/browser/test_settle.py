"""Unit tests for the network tracker used by settle detection."""

from services.ingestion.tools.browser import protocol
from services.ingestion.tools.browser.settle import NetworkTracker


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


class FakeRequest:
    def __init__(self, resource_type="fetch"):
        self.resource_type = resource_type


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_tracker_busy_while_request_in_flight_then_quiet():
    page, clock = FakePage(), Clock()
    tracker = NetworkTracker(page, clock=clock)
    clock.t += 10
    assert not tracker.busy()
    req = FakeRequest()
    page.handlers["request"](req)
    clock.t += 1
    assert tracker.busy()
    page.handlers["requestfinished"](req)
    clock.t += protocol.QUIET_S / 2
    assert tracker.busy()  # recent activity
    clock.t += protocol.QUIET_S
    assert not tracker.busy()


def test_tracker_ignores_long_requests_and_event_streams():
    page, clock = FakePage(), Clock()
    tracker = NetworkTracker(page, clock=clock)
    page.handlers["request"](FakeRequest("eventsource"))
    page.handlers["request"](FakeRequest("websocket"))
    clock.t += 1
    assert not tracker.busy()
    page.handlers["request"](FakeRequest("fetch"))  # long-poll that never finishes
    clock.t += protocol.LONG_REQUEST_S + 0.1
    assert not tracker.busy()


def test_tracker_failed_request_ends_it():
    page, clock = FakePage(), Clock()
    tracker = NetworkTracker(page, clock=clock)
    req = FakeRequest()
    page.handlers["request"](req)
    page.handlers["requestfailed"](req)
    clock.t += 1
    assert not tracker.busy()
