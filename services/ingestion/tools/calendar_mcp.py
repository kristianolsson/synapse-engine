#!/usr/bin/env python3
"""
Google Calendar MCP Server for synapse-engine.

Exposes the calendar_cli functions as MCP tools so that AI agents (e.g. Gemini CLI)
can interact with Google Calendar via the Model Context Protocol instead of
shelling out to the CLI.

Running standalone:
    python calendar_mcp.py

Registering with Gemini CLI (project-level .gemini/settings.json):
    {
      "mcpServers": {
        "calendar": {
          "command": "/path/to/venv/bin/python3",
          "args": ["/path/to/calendar_mcp.py"]
        }
      }
    }
"""

import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Allows `python calendar_mcp.py` to resolve the `services.*` package when run
# standalone, matching every other tool CLI's bootstrap (etrade_cli.py,
# options_bot_cli.py, amazon_fresh_cli.py, reminder_cli.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from services.ingestion.tools.calendar_cli import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_TOKEN_PATH,
    build_service,
    cmd_add_event,
    cmd_delete_event,
    cmd_edit_event,
    cmd_list_calendars,
    cmd_list_events,
    load_calendars,
)

mcp = FastMCP("Google Calendar")

# --- Lazy-initialized shared state ---------------------------------------------------

_calendars: Optional[list] = None
_service = None


def _get_calendars() -> list[dict]:
    """Load calendar config on first use."""
    global _calendars
    if _calendars is None:
        _calendars = load_calendars(DEFAULT_CONFIG_PATH)
    return _calendars


def _get_service():
    """Build the Google Calendar API service on first use."""
    global _service
    if _service is None:
        _service = build_service(DEFAULT_TOKEN_PATH, DEFAULT_CREDENTIALS_PATH)
    return _service


# --- MCP Tools ------------------------------------------------------------------------


@mcp.tool()
def list_events(days: int = 7, date: str = "", calendar: str = "") -> str:
    """List upcoming calendar events across all configured calendars.

    Args:
        days: Number of days ahead to query (default 7). Ignored if date is set.
        date: Specific date to query in YYYY-MM-DD format. Overrides days.
        calendar: Filter to a specific calendar by label (e.g. "Maria", "Kids Sports").

    Returns:
        Events grouped by date, with times, titles, calendar labels, and locations.
    """
    return cmd_list_events(
        days=days, date=date, calendar=calendar,
        calendars=_get_calendars(), service=_get_service(),
    )


@mcp.tool()
def add_event(title: str, start: str, end: str, description: str = "",
              guests: str = "") -> str:
    """Create a new event on the primary calendar.

    Args:
        title: Event title.
        start: Start time in ISO 8601 format (e.g. "2026-03-15T10:00:00") or YYYY-MM-DD for all-day.
        end: End time in ISO 8601 format or YYYY-MM-DD for all-day.
        description: Optional event description or notes.
        guests: Optional comma-separated guest email addresses.

    Returns:
        Confirmation with event details and a link.
    """
    return cmd_add_event(
        title=title, start=start, end=end,
        description=description, guests=guests,
        calendars=_get_calendars(), service=_get_service(),
    )


@mcp.tool()
def edit_event(event_id: str, title: str = "", start: str = "",
               end: str = "", description: str = "", guests: str = "") -> str:
    """Edit an existing event on the primary calendar.

    Only the provided fields will be updated; omitted fields remain unchanged.

    Args:
        event_id: The event ID (shown in list_events output for primary calendar events).
        title: New event title (leave empty to keep current).
        start: New start time in ISO 8601 format (leave empty to keep current).
        end: New end time in ISO 8601 format (leave empty to keep current).
        description: New description (leave empty to keep current).
        guests: New comma-separated guest email addresses (leave empty to keep current).

    Returns:
        Confirmation with updated event details.
    """
    return cmd_edit_event(
        event_id=event_id, title=title, start=start,
        end=end, description=description, guests=guests,
        calendars=_get_calendars(), service=_get_service(),
    )


@mcp.tool()
def delete_event(event_id: str) -> str:
    """Delete an event from the primary calendar.

    Args:
        event_id: The event ID to delete (shown in list_events output).

    Returns:
        Confirmation that the event was deleted.
    """
    return cmd_delete_event(
        event_id=event_id,
        calendars=_get_calendars(), service=_get_service(),
    )


@mcp.tool()
def list_calendars() -> str:
    """List all configured calendars and their access levels.

    Returns:
        List of calendars with labels, access levels, and calendar IDs.
    """
    return cmd_list_calendars(_get_calendars())


if __name__ == "__main__":
    mcp.run()
