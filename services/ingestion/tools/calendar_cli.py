#!/usr/bin/env python3
"""
Google Calendar CLI tool for synapse-engine.

Provides subcommands to list events across multiple calendars,
create/edit/delete events on the primary calendar, and list configured calendars.

Usage:
    python calendar_cli.py list-events [--days N] [--date YYYY-MM-DD] [--calendar LABEL]
    python calendar_cli.py add-event --title "..." --start "ISO" --end "ISO" [--description "..."] [--guests "e1,e2"]
    python calendar_cli.py edit-event --event-id "ID" [--title "..."] [--start "ISO"] [--end "ISO"] [--description "..."] [--guests "e1,e2"]
    python calendar_cli.py delete-event --event-id "ID"
    python calendar_cli.py list-calendars
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Scopes needed for read + write access to calendars
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Local timezone for display — all events are normalized to this
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# Default paths (can be overridden via env or args)
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_CONFIG_PATH = _ROOT / "calendars.json"
DEFAULT_TOKEN_PATH = _ROOT / "token.json"
DEFAULT_CREDENTIALS_PATH = _ROOT / "credentials.json"


def _parse_and_localize(dt_str: str) -> datetime:
    """Parse an ISO datetime string and convert to local timezone."""
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    # If naive (no tz info), assume it's already local
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def load_calendars(config_path: Path) -> list[dict]:
    """Load calendar configuration from calendars.json.

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        ValueError: If the config is not a JSON array.
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"Calendar config not found at {config_path}. "
            "Run setup_calendar.py first, or create calendars.json."
        )

    with open(config_path) as f:
        calendars = json.load(f)

    if not isinstance(calendars, list):
        raise ValueError("calendars.json must be a JSON array.")

    return calendars


def get_credentials(token_path: Path, credentials_path: Path) -> Credentials:
    """Load or refresh OAuth2 credentials.

    Raises:
        FileNotFoundError: If credentials.json is missing and no valid token exists.
        RuntimeError: If credentials cannot be obtained.
    """
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"OAuth credentials not found at {credentials_path}\n"
                    "Download credentials.json from Google Cloud Console,\n"
                    "then run setup_calendar.py to authenticate."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save refreshed/new token
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def build_service(token_path: Path, credentials_path: Path):
    """Build a Google Calendar API service client."""
    creds = get_credentials(token_path, credentials_path)
    return build("calendar", "v3", credentials=creds)


def _get_primary_calendar(calendars: list[dict]) -> dict:
    """Find and return the primary calendar.

    Raises:
        ValueError: If no calendar with access 'primary' is found.
    """
    for cal in calendars:
        if cal.get("access") == "primary":
            return cal
    raise ValueError("No calendar with access 'primary' found in config.")


def cmd_list_events(days: int = 7, date: str = "", calendar: str = "",
                    calendars: list[dict] = None, service=None) -> str:
    """List events across all configured calendars.

    Args:
        days: Number of days ahead to query (default 7).
        date: Specific date to query (YYYY-MM-DD). Overrides days if set.
        calendar: Filter to a specific calendar by label.
        calendars: Calendar config list.
        service: Google Calendar API service client.

    Returns:
        Formatted string of events grouped by date.

    Raises:
        ValueError: If date format is invalid or calendar label not found.
    """
    out = StringIO()

    # Determine time range
    if date:
        try:
            target = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid date format '{date}'. Use YYYY-MM-DD.")
        time_min = target.replace(tzinfo=LOCAL_TZ).isoformat()
        time_max = (target + timedelta(days=1)).replace(tzinfo=LOCAL_TZ).isoformat()
        range_desc = date
    else:
        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()
        range_desc = f"the next {days} day(s)"

    # Filter calendars if --calendar specified
    cal_list = list(calendars) if calendars else []
    if calendar:
        filter_label = calendar.lower()
        filtered = [c for c in cal_list if c.get("label", c["id"]).lower() == filter_label]
        if not filtered:
            raise ValueError(f"No calendar matching '{calendar}'. Use list-calendars to see available.")
        cal_list = filtered

    all_events = []

    for cal in cal_list:
        cal_id = cal["id"]
        label = cal.get("label", cal_id)
        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            ).execute()

            for event in result.get("items", []):
                start_raw = event["start"].get("dateTime", event["start"].get("date", ""))
                end_raw = event["end"].get("dateTime", event["end"].get("date", ""))
                is_all_day = "dateTime" not in event["start"]

                all_events.append({
                    "start_raw": start_raw,
                    "end_raw": end_raw,
                    "is_all_day": is_all_day,
                    "event_id": event.get("id", ""),
                    "summary": event.get("summary", "(No title)"),
                    "calendar": label,
                    "calendar_access": cal.get("access", "readonly"),
                    "location": event.get("location", ""),
                    "description": event.get("description", ""),
                })
        except Exception as e:
            out.write(f"Warning: Could not fetch events from '{label}': {e}\n")

    # Sort: normalize all datetimes to local tz for correct ordering
    def sort_key(e):
        if e["is_all_day"]:
            return e["start_raw"] + "T00:00:00"
        return _parse_and_localize(e["start_raw"]).isoformat()

    all_events.sort(key=sort_key)

    if not all_events:
        out.write(f"No events found for {range_desc}.")
        return out.getvalue()

    # Group by local date and format
    current_date = None
    for event in all_events:
        if event["is_all_day"]:
            start_date = event["start_raw"][:10]
            end_date = event["end_raw"][:10]
            local_date = start_date
            # Show date range for multi-day events
            if end_date > start_date:
                # Google uses exclusive end date for all-day events, so subtract 1 day
                end_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=1)
                actual_end = end_dt.strftime("%Y-%m-%d")
                if actual_end > start_date:
                    start_fmt = datetime.strptime(start_date, "%Y-%m-%d").strftime("%b %d")
                    end_fmt = end_dt.strftime("%b %d")
                    time_str = f"{start_fmt} - {end_fmt}"
                else:
                    time_str = ""
            else:
                time_str = ""
        else:
            local_dt = _parse_and_localize(event["start_raw"])
            local_end_dt = _parse_and_localize(event["end_raw"])
            local_date = local_dt.strftime("%Y-%m-%d")
            time_str = local_dt.strftime("%I:%M %p")
            # Show end date if the event spans multiple days
            if local_end_dt.strftime("%Y-%m-%d") > local_date:
                end_fmt = local_end_dt.strftime("%b %d %I:%M %p")
                time_str = f"{local_dt.strftime('%b %d')} {time_str} - {end_fmt}"

        if local_date != current_date:
            current_date = local_date
            try:
                dt = datetime.strptime(local_date, "%Y-%m-%d")
                out.write(f"\n## {dt.strftime('%A, %B %d, %Y')}\n")
            except ValueError:
                out.write(f"\n## {local_date}\n")

        location = f" — {event['location']}" if event["location"] else ""
        event_id = f" (id: {event['event_id']})" if event["event_id"] and event["calendar_access"] == "primary" else ""
        cal_label = f" [{event['calendar']}]" if event["calendar_access"] != "primary" else ""
        description = f"\n  {event['description']}" if event["description"] else ""
        out.write(f"- {time_str} | {event['summary']}{cal_label}{location}{event_id}{description}\n")

    return out.getvalue()


def cmd_add_event(title: str, start: str, end: str, description: str = "",
                  guests: str = "", calendars: list[dict] = None,
                  service=None) -> str:
    """Create an event on the primary calendar.

    Args:
        title: Event title.
        start: Start time (ISO 8601 or YYYY-MM-DD for all-day).
        end: End time (ISO 8601 or YYYY-MM-DD for all-day).
        description: Optional event description.
        guests: Optional comma-separated guest emails.
        calendars: Calendar config list.
        service: Google Calendar API service client.

    Returns:
        Formatted confirmation string.

    Raises:
        ValueError: If no primary calendar is configured.
        RuntimeError: If the API call fails.
    """
    primary = _get_primary_calendar(calendars or [])
    out = StringIO()

    event_body = {
        "summary": title,
    }
    if len(start) == 10:
        event_body["start"] = {"date": start}
    else:
        event_body["start"] = {"dateTime": start, "timeZone": "America/Los_Angeles"}
    if len(end) == 10:
        event_body["end"] = {"date": end}
    else:
        event_body["end"] = {"dateTime": end, "timeZone": "America/Los_Angeles"}

    if description:
        event_body["description"] = description

    guest_emails = []
    if guests:
        guest_emails = [g.strip() for g in guests.split(",") if g.strip()]
        event_body["attendees"] = [{"email": e} for e in guest_emails]

    try:
        created = service.events().insert(
            calendarId=primary["id"],
            body=event_body,
            sendUpdates="all" if guest_emails else "none",
        ).execute()

        out.write(f"Event created: {created.get('summary', title)}\n")
        out.write(f"When: {start} to {end}\n")
        if guest_emails:
            out.write(f"Guests: {guests}\n")
        out.write(f"Link: {created.get('htmlLink', 'N/A')}\n")
    except Exception as e:
        raise RuntimeError(f"Error creating event: {e}")

    return out.getvalue()


def cmd_edit_event(event_id: str, title: str = "", start: str = "",
                   end: str = "", description: str = "", guests: str = "",
                   calendars: list[dict] = None, service=None) -> str:
    """Edit an existing event on the primary calendar.

    Args:
        event_id: The event ID to edit.
        title: New event title (empty = no change).
        start: New start time (empty = no change).
        end: New end time (empty = no change).
        description: New description (empty = no change).
        guests: New comma-separated guest emails (empty = no change).
        calendars: Calendar config list.
        service: Google Calendar API service client.

    Returns:
        Formatted confirmation string.

    Raises:
        ValueError: If no primary calendar is configured.
        RuntimeError: If the event is not found or update fails.
    """
    primary = _get_primary_calendar(calendars or [])
    out = StringIO()

    # Fetch the existing event first
    try:
        existing = service.events().get(
            calendarId=primary["id"], eventId=event_id
        ).execute()
    except Exception as e:
        raise RuntimeError(f"Could not find event '{event_id}': {e}")

    # Apply updates — only override fields that were explicitly provided
    if title:
        existing["summary"] = title
    if start:
        if len(start) == 10:
            existing["start"] = {"date": start}
        else:
            existing["start"] = {"dateTime": start, "timeZone": "America/Los_Angeles"}
    if end:
        if len(end) == 10:
            existing["end"] = {"date": end}
        else:
            existing["end"] = {"dateTime": end, "timeZone": "America/Los_Angeles"}
    if description:
        existing["description"] = description

    guest_emails = []
    if guests:
        guest_emails = [g.strip() for g in guests.split(",") if g.strip()]
        existing["attendees"] = [{"email": e} for e in guest_emails]

    try:
        updated = service.events().update(
            calendarId=primary["id"],
            eventId=event_id,
            body=existing,
            sendUpdates="all" if guest_emails else "none",
        ).execute()

        out.write(f"Event updated: {updated.get('summary', '')}\n")
        evt_start = updated.get("start", {}).get("dateTime", updated.get("start", {}).get("date", ""))
        evt_end = updated.get("end", {}).get("dateTime", updated.get("end", {}).get("date", ""))
        out.write(f"When: {evt_start} to {evt_end}\n")
        out.write(f"Link: {updated.get('htmlLink', 'N/A')}\n")
    except Exception as e:
        raise RuntimeError(f"Error updating event: {e}")

    return out.getvalue()


def cmd_delete_event(event_id: str, calendars: list[dict] = None,
                     service=None) -> str:
    """Delete an event from the primary calendar.

    Args:
        event_id: The event ID to delete.
        calendars: Calendar config list.
        service: Google Calendar API service client.

    Returns:
        Confirmation string.

    Raises:
        ValueError: If no primary calendar is configured.
        RuntimeError: If the API call fails.
    """
    primary = _get_primary_calendar(calendars or [])

    try:
        service.events().delete(
            calendarId=primary["id"],
            eventId=event_id,
            sendUpdates="all",
        ).execute()
        return f"Event deleted: {event_id}"
    except Exception as e:
        raise RuntimeError(f"Error deleting event: {e}")


def cmd_list_calendars(calendars: list[dict]) -> str:
    """Show configured calendars.

    Args:
        calendars: Calendar config list.

    Returns:
        Formatted listing of configured calendars.
    """
    out = StringIO()
    out.write("Configured calendars:\n\n")
    for cal in calendars:
        label = cal.get("label", cal["id"])
        access = cal.get("access", "unknown")
        icon = "✏️" if access == "primary" else "👁️"
        out.write(f"  {icon} {label} ({access}) — {cal['id']}\n")
    return out.getvalue()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Google Calendar CLI for synapse-engine"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help="Path to calendars.json"
    )
    parser.add_argument(
        "--token", type=Path, default=DEFAULT_TOKEN_PATH,
        help="Path to token.json"
    )
    parser.add_argument(
        "--credentials", type=Path, default=DEFAULT_CREDENTIALS_PATH,
        help="Path to credentials.json"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # list-events
    p_list = sub.add_parser("list-events", help="List upcoming events")
    p_list.add_argument("--days", type=int, default=7, help="Number of days ahead (default: 7)")
    p_list.add_argument("--date", default="", help="Specific date to query (YYYY-MM-DD)")
    p_list.add_argument("--calendar", default="", help="Filter to a specific calendar by label")

    # add-event
    p_add = sub.add_parser("add-event", help="Create a new event")
    p_add.add_argument("--title", required=True, help="Event title")
    p_add.add_argument("--start", required=True, help="Start time (ISO 8601)")
    p_add.add_argument("--end", required=True, help="End time (ISO 8601)")
    p_add.add_argument("--description", default="", help="Event description")
    p_add.add_argument("--guests", default="", help="Comma-separated guest emails")

    # edit-event
    p_edit = sub.add_parser("edit-event", help="Edit an existing event")
    p_edit.add_argument("--event-id", required=True, help="Event ID (from list-events output)")
    p_edit.add_argument("--title", default="", help="New event title")
    p_edit.add_argument("--start", default="", help="New start time (ISO 8601)")
    p_edit.add_argument("--end", default="", help="New end time (ISO 8601)")
    p_edit.add_argument("--description", default="", help="New event description")
    p_edit.add_argument("--guests", default="", help="New comma-separated guest emails")

    # delete-event
    p_del = sub.add_parser("delete-event", help="Delete an event")
    p_del.add_argument("--event-id", required=True, help="Event ID (from list-events output)")

    # list-calendars
    sub.add_parser("list-calendars", help="Show configured calendars")

    args = parser.parse_args(argv)

    try:
        calendars = load_calendars(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.command == "list-calendars":
        print(cmd_list_calendars(calendars))
        return

    # Commands that need the API
    service = build_service(args.token, args.credentials)

    try:
        if args.command == "list-events":
            result = cmd_list_events(
                days=args.days, date=args.date, calendar=args.calendar,
                calendars=calendars, service=service,
            )
        elif args.command == "add-event":
            result = cmd_add_event(
                title=args.title, start=args.start, end=args.end,
                description=args.description, guests=args.guests,
                calendars=calendars, service=service,
            )
        elif args.command == "edit-event":
            result = cmd_edit_event(
                event_id=args.event_id, title=args.title, start=args.start,
                end=args.end, description=args.description, guests=args.guests,
                calendars=calendars, service=service,
            )
        elif args.command == "delete-event":
            result = cmd_delete_event(
                event_id=args.event_id,
                calendars=calendars, service=service,
            )
        else:
            return

        print(result)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
