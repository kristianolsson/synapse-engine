"""
Unit tests for the Google Calendar CLI tool.

All tests mock the Google Calendar API — no real API calls are made.
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def sample_calendars():
    """Sample calendar config."""
    return [
        {"id": "primary", "label": "Kristian", "access": "primary"},
        {"id": "wife@gmail.com", "label": "Sarah", "access": "readonly"},
        {"id": "sports@group.calendar.google.com", "label": "Kids Sports", "access": "readonly"},
    ]


@pytest.fixture
def sample_calendars_file(tmp_path, sample_calendars):
    """Create a temporary calendars.json file."""
    config_path = tmp_path / "calendars.json"
    config_path.write_text(json.dumps(sample_calendars))
    return config_path


@pytest.fixture
def mock_token_file(tmp_path):
    """Create a fake token.json path."""
    return tmp_path / "token.json"


@pytest.fixture
def mock_credentials_file(tmp_path):
    """Create a fake credentials.json path."""
    return tmp_path / "credentials.json"


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
        if "services.ingestion.tools.calendar_cli" in sys.modules:
            del sys.modules["services.ingestion.tools.calendar_cli"]

        from services.ingestion.tools import calendar_cli

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


class TestLoadCalendars:
    """Tests for loading calendar configuration."""

    def test_load_valid_config(self, sample_calendars_file, sample_calendars, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        result = cal_cli.load_calendars(sample_calendars_file)
        assert result == sample_calendars
        assert len(result) == 3

    def test_load_missing_config(self, tmp_path, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        missing_path = tmp_path / "nonexistent.json"
        with pytest.raises(SystemExit):
            cal_cli.load_calendars(missing_path)

    def test_load_invalid_json(self, tmp_path, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json")
        with pytest.raises(Exception):
            cal_cli.load_calendars(bad_file)

    def test_load_non_array_json(self, tmp_path, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        bad_file = tmp_path / "object.json"
        bad_file.write_text('{"id": "primary"}')
        with pytest.raises(SystemExit):
            cal_cli.load_calendars(bad_file)


class TestListCalendars:
    """Tests for the list-calendars command."""

    def test_list_calendars_output(self, sample_calendars, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        cal_cli.cmd_list_calendars(sample_calendars)
        captured = capsys.readouterr()
        assert "Kristian" in captured.out
        assert "primary" in captured.out
        assert "Sarah" in captured.out
        assert "readonly" in captured.out
        assert "Kids Sports" in captured.out


class TestListEvents:
    """Tests for the list-events command."""

    def _make_args(self, days=7, date="", calendar=""):
        args = mock.MagicMock()
        args.days = days
        args.date = date
        args.calendar = calendar
        return args

    def test_list_events_aggregates_all_calendars(
        self, sample_calendars, mock_google_modules, capsys
    ):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        # Mock API responses for each calendar
        events_cal1 = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-01T10:00:00-08:00"},
                    "end": {"dateTime": "2026-03-01T11:00:00-08:00"},
                    "summary": "Team standup",
                },
            ]
        }
        events_cal2 = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-01T09:00:00-08:00"},
                    "end": {"dateTime": "2026-03-01T09:30:00-08:00"},
                    "summary": "School dropoff",
                },
            ]
        }
        events_cal3 = {
            "items": [
                {
                    "start": {"dateTime": "2026-03-02T15:00:00-08:00"},
                    "end": {"dateTime": "2026-03-02T17:00:00-08:00"},
                    "summary": "Soccer practice",
                },
            ]
        }

        # Make the mock return different results per call
        service.events().list().execute.side_effect = [events_cal1, events_cal2, events_cal3]

        cal_cli.cmd_list_events(self._make_args(), sample_calendars, service)
        captured = capsys.readouterr()

        # All events should appear
        assert "Team standup" in captured.out
        assert "School dropoff" in captured.out
        assert "Soccer practice" in captured.out

        # Calendar labels should appear
        assert "Kristian" in captured.out
        assert "Sarah" in captured.out
        assert "Kids Sports" in captured.out

    def test_list_events_empty(self, sample_calendars, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        # All calendars return empty
        service.events().list().execute.side_effect = [
            {"items": []},
            {"items": []},
            {"items": []},
        ]

        cal_cli.cmd_list_events(self._make_args(), sample_calendars, service)
        captured = capsys.readouterr()
        assert "No events found" in captured.out

    def test_list_events_api_error_graceful(
        self, sample_calendars, mock_google_modules, capsys
    ):
        """When one calendar's API call fails, other calendars still return results."""
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        # First calendar succeeds, second fails, third succeeds
        service.events().list().execute.side_effect = [
            {
                "items": [{
                    "start": {"dateTime": "2026-03-01T10:00:00-08:00"},
                    "end": {"dateTime": "2026-03-01T11:00:00-08:00"},
                    "summary": "Meeting",
                }]
            },
            Exception("API error"),
            {"items": []},
        ]

        cal_cli.cmd_list_events(self._make_args(), sample_calendars, service)
        captured = capsys.readouterr()

        # Should still show the successful calendar's events
        assert "Meeting" in captured.out


class TestAddEvent:
    """Tests for the add-event command."""

    def _make_args(self, title="Test", start="2026-03-01T10:00:00", end="2026-03-01T11:00:00",
                   description="", guests=""):
        args = mock.MagicMock()
        args.title = title
        args.start = start
        args.end = end
        args.description = description
        args.guests = guests
        return args

    def test_add_event_primary_calendar(self, sample_calendars, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        service.events().insert().execute.return_value = {
            "summary": "Dentist appointment",
            "htmlLink": "https://calendar.google.com/event/123",
        }

        args = self._make_args(title="Dentist appointment")
        cal_cli.cmd_add_event(args, sample_calendars, service)
        captured = capsys.readouterr()

        assert "Event created" in captured.out
        assert "Dentist appointment" in captured.out

        # Verify it targeted the primary calendar
        service.events().insert.assert_called()
        call_kwargs = service.events().insert.call_args
        assert call_kwargs[1]["calendarId"] == "primary"

    def test_add_event_with_guests(self, sample_calendars, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        service.events().insert().execute.return_value = {
            "summary": "Dinner",
            "htmlLink": "https://calendar.google.com/event/456",
        }

        args = self._make_args(title="Dinner", guests="wife@gmail.com,friend@gmail.com")
        cal_cli.cmd_add_event(args, sample_calendars, service)
        captured = capsys.readouterr()

        assert "Guests" in captured.out
        # Verify attendees were sent
        call_kwargs = service.events().insert.call_args
        event_body = call_kwargs[1]["body"]
        assert "attendees" in event_body
        assert len(event_body["attendees"]) == 2
        # Send updates should be "all" when guests are present
        assert call_kwargs[1]["sendUpdates"] == "all"

    def test_add_event_with_description(self, sample_calendars, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        service.events().insert().execute.return_value = {
            "summary": "Meeting",
            "htmlLink": "https://calendar.google.com/event/789",
        }

        desc = "Forwarded email content here..."
        args = self._make_args(title="Meeting", description=desc)
        cal_cli.cmd_add_event(args, sample_calendars, service)

        call_kwargs = service.events().insert.call_args
        event_body = call_kwargs[1]["body"]
        assert event_body["description"] == desc

    def test_add_event_no_primary_calendar(self, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        # Only readonly calendars
        readonly_only = [
            {"id": "cal1@gmail.com", "label": "Cal1", "access": "readonly"},
        ]

        args = self._make_args(title="Test")
        with pytest.raises(SystemExit):
            cal_cli.cmd_add_event(args, readonly_only, service)

    def test_add_event_api_error(self, sample_calendars, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        service.events().insert().execute.side_effect = Exception("API Error")

        args = self._make_args(title="Test")
        with pytest.raises(SystemExit):
            cal_cli.cmd_add_event(args, sample_calendars, service)


class TestEditEvent:
    """Tests for the edit-event command."""

    def _make_args(self, event_id="abc123", title="", start="", end="",
                   description="", guests=""):
        args = mock.MagicMock()
        args.event_id = event_id
        args.title = title
        args.start = start
        args.end = end
        args.description = description
        args.guests = guests
        return args

    def test_edit_event_title(self, sample_calendars, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

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

        args = self._make_args(title="New Title")
        cal_cli.cmd_edit_event(args, sample_calendars, service)
        captured = capsys.readouterr()
        assert "Event updated" in captured.out
        assert "New Title" in captured.out

    def test_edit_event_not_found(self, sample_calendars, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        service.events().get().execute.side_effect = Exception("Not found")

        args = self._make_args(event_id="nonexistent")
        with pytest.raises(SystemExit):
            cal_cli.cmd_edit_event(args, sample_calendars, service)

    def test_edit_event_no_primary(self, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        readonly_only = [{"id": "cal1", "label": "Cal1", "access": "readonly"}]
        args = self._make_args(title="Test")
        with pytest.raises(SystemExit):
            cal_cli.cmd_edit_event(args, readonly_only, service)


class TestDeleteEvent:
    """Tests for the delete-event command."""

    def _make_args(self, event_id="abc123"):
        args = mock.MagicMock()
        args.event_id = event_id
        return args

    def test_delete_event_success(self, sample_calendars, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        service.events().delete().execute.return_value = None

        args = self._make_args()
        cal_cli.cmd_delete_event(args, sample_calendars, service)
        captured = capsys.readouterr()
        assert "Event deleted" in captured.out

    def test_delete_event_api_error(self, sample_calendars, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        service.events().delete().execute.side_effect = Exception("API Error")

        args = self._make_args()
        with pytest.raises(SystemExit):
            cal_cli.cmd_delete_event(args, sample_calendars, service)

    def test_delete_event_no_primary(self, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        service = mock_google_modules["service"]

        readonly_only = [{"id": "cal1", "label": "Cal1", "access": "readonly"}]
        args = self._make_args()
        with pytest.raises(SystemExit):
            cal_cli.cmd_delete_event(args, readonly_only, service)


class TestMainEntrypoint:
    """Tests for the CLI argument parsing and routing."""

    def test_list_calendars_command(self, sample_calendars_file, mock_google_modules, capsys):
        cal_cli = mock_google_modules["module"]
        cal_cli.main([
            "--config", str(sample_calendars_file),
            "list-calendars",
        ])
        captured = capsys.readouterr()
        assert "Kristian" in captured.out

    def test_missing_command(self, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        with pytest.raises(SystemExit):
            cal_cli.main([])

    def test_add_event_missing_required_args(self, sample_calendars_file, mock_google_modules):
        cal_cli = mock_google_modules["module"]
        with pytest.raises(SystemExit):
            cal_cli.main([
                "--config", str(sample_calendars_file),
                "add-event",
                # Missing --title, --start, --end
            ])
