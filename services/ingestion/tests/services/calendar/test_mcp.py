"""
Unit tests for the Google Calendar MCP server.

Tests verify that the MCP tool handlers correctly call the refactored
calendar_cli functions and return appropriate results. All Google API
interactions are mocked.
"""

import sys
from unittest import mock

import pytest


@pytest.fixture
def mock_google_modules():
    """Mock all google API modules so tests work without google packages installed."""
    mock_creds = mock.MagicMock()
    mock_creds.valid = True

    mock_credentials_mod = mock.MagicMock()
    mock_credentials_mod.Credentials.from_authorized_user_file.return_value = mock_creds

    mock_request_mod = mock.MagicMock()
    mock_flow_mod = mock.MagicMock()

    mock_service = mock.MagicMock()
    mock_build = mock.MagicMock(return_value=mock_service)

    patches = {
        "google.oauth2.credentials": mock_credentials_mod,
        "google.oauth2": mock.MagicMock(),
        "google.auth.transport.requests": mock_request_mod,
        "google.auth.transport": mock.MagicMock(),
        "google.auth": mock.MagicMock(),
        "google": mock.MagicMock(),
        "google_auth_oauthlib.flow": mock_flow_mod,
        "google_auth_oauthlib": mock.MagicMock(),
        "googleapiclient.discovery": mock.MagicMock(build=mock_build),
        "googleapiclient": mock.MagicMock(),
    }

    with mock.patch.dict(sys.modules, patches):
        # Force re-import with mocked dependencies
        for mod_name in list(sys.modules):
            if mod_name.startswith("services.ingestion.services.calendar"):
                del sys.modules[mod_name]

        from services.ingestion.services.calendar import cli as calendar_cli

        # Patch the module's references
        calendar_cli.Credentials = mock_credentials_mod.Credentials
        calendar_cli.Request = mock_request_mod.Request
        calendar_cli.InstalledAppFlow = mock_flow_mod.InstalledAppFlow
        calendar_cli.build = mock_build

        yield {
            "module": calendar_cli,
            "service": mock_service,
            "build": mock_build,
            "creds": mock_creds,
            "credentials_mod": mock_credentials_mod,
        }


@pytest.fixture
def sample_calendars():
    """Sample calendar config."""
    return [
        {"id": "primary", "label": "Kristian", "access": "primary"},
        {"id": "wife@gmail.com", "label": "Sarah", "access": "readonly"},
    ]


@pytest.fixture
def mcp_tools(mock_google_modules, sample_calendars, tmp_path):
    """Import calendar_mcp and wire up mock calendars + service."""
    # Force re-import
    for mod_name in list(sys.modules):
        if "calendar_mcp" in mod_name:
            del sys.modules[mod_name]

    from services.ingestion.services.calendar import mcp as calendar_mcp

    # Inject mocked state so tools don't try to load real files or auth
    calendar_mcp._calendars = sample_calendars
    calendar_mcp._service = mock_google_modules["service"]

    return {
        "mcp_module": calendar_mcp,
        "service": mock_google_modules["service"],
        "calendars": sample_calendars,
    }


class TestMCPListEvents:
    """Tests for the list_events MCP tool."""

    def test_list_events_returns_string(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().list().execute.side_effect = [
            {"items": [{
                "start": {"dateTime": "2026-03-01T10:00:00-08:00"},
                "end": {"dateTime": "2026-03-01T11:00:00-08:00"},
                "summary": "Standup",
            }]},
            {"items": []},
        ]

        result = mcp.list_events(days=7)
        assert isinstance(result, str)
        assert "Standup" in result

    def test_list_events_with_date_filter(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().list().execute.side_effect = [
            {"items": []},
            {"items": []},
        ]

        result = mcp.list_events(date="2026-03-15")
        assert isinstance(result, str)
        assert "No events found" in result

    def test_list_events_with_calendar_filter(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().list().execute.side_effect = [
            {"items": [{
                "start": {"dateTime": "2026-03-01T09:00:00-08:00"},
                "end": {"dateTime": "2026-03-01T09:30:00-08:00"},
                "summary": "School dropoff",
            }]},
        ]

        result = mcp.list_events(calendar="Sarah")
        assert "School dropoff" in result


class TestMCPAddEvent:
    """Tests for the add_event MCP tool."""

    def test_add_event_returns_confirmation(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().insert().execute.return_value = {
            "summary": "Dentist",
            "htmlLink": "https://calendar.google.com/event/123",
        }

        result = mcp.add_event(
            title="Dentist",
            start="2026-03-15T10:00:00",
            end="2026-03-15T11:00:00",
        )

        assert isinstance(result, str)
        assert "Event created" in result
        assert "Dentist" in result

    def test_add_event_api_error_raises(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().insert().execute.side_effect = Exception("API Error")

        with pytest.raises(RuntimeError):
            mcp.add_event(
                title="Test",
                start="2026-03-15T10:00:00",
                end="2026-03-15T11:00:00",
            )


class TestMCPEditEvent:
    """Tests for the edit_event MCP tool."""

    def test_edit_event_returns_confirmation(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().get().execute.return_value = {
            "summary": "Old Title",
            "start": {"dateTime": "2026-03-01T10:00:00"},
            "end": {"dateTime": "2026-03-01T11:00:00"},
        }
        service.events().update().execute.return_value = {
            "summary": "New Title",
            "start": {"dateTime": "2026-03-01T10:00:00"},
            "end": {"dateTime": "2026-03-01T11:00:00"},
            "htmlLink": "https://calendar.google.com/event/123",
        }

        result = mcp.edit_event(event_id="abc123", title="New Title")
        assert "Event updated" in result
        assert "New Title" in result

    def test_edit_event_not_found_raises(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().get().execute.side_effect = Exception("Not found")

        with pytest.raises(RuntimeError):
            mcp.edit_event(event_id="nonexistent", title="Test")


class TestMCPDeleteEvent:
    """Tests for the delete_event MCP tool."""

    def test_delete_event_returns_confirmation(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().delete().execute.return_value = None

        result = mcp.delete_event(event_id="abc123")
        assert "Event deleted" in result
        assert "abc123" in result

    def test_delete_event_api_error_raises(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]
        service = mcp_tools["service"]

        service.events().delete().execute.side_effect = Exception("API Error")

        with pytest.raises(RuntimeError):
            mcp.delete_event(event_id="abc123")


class TestMCPListCalendars:
    """Tests for the list_calendars MCP tool."""

    def test_list_calendars_returns_string(self, mcp_tools):
        mcp = mcp_tools["mcp_module"]

        result = mcp.list_calendars()
        assert isinstance(result, str)
        assert "Kristian" in result
        assert "primary" in result
        assert "Sarah" in result
