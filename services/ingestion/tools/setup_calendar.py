#!/usr/bin/env python3
"""
One-time setup script for Google Calendar OAuth2 authentication.

Usage:
    1. Download credentials.json from Google Cloud Console
       (APIs & Services > Credentials > OAuth 2.0 Client IDs > Download JSON)
    2. Place it in the synapse-engine root directory
    3. Run: python -m services.ingestion.tools.setup_calendar
    4. Complete the browser OAuth flow
    5. token.json will be saved automatically

Required Google Cloud setup:
    - Create a project in Google Cloud Console
    - Enable the Google Calendar API
    - Create OAuth 2.0 Client ID (Desktop application)
    - Download the credentials JSON
"""

import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_TOKEN_PATH = _ROOT / "token.json"
DEFAULT_CREDENTIALS_PATH = _ROOT / "credentials.json"


def setup(
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
) -> None:
    """Run the OAuth2 setup flow and verify calendar access."""

    if not credentials_path.exists():
        print(f"Error: credentials.json not found at {credentials_path}")
        print()
        print("To set up Google Calendar access:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create an OAuth 2.0 Client ID (type: Desktop application)")
        print("  3. Download the JSON and save as: credentials.json")
        print(f"  4. Place it at: {credentials_path}")
        sys.exit(1)

    # Check for existing valid token
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        print("Already authenticated. Token is valid.")
    elif creds and creds.expired and creds.refresh_token:
        print("Refreshing expired token...")
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        print("Token refreshed.")
    else:
        print("Starting OAuth2 flow — a browser window will open...")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), SCOPES
        )
        creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        print(f"Token saved to {token_path}")

    # Verify by listing calendars
    print("\nVerifying access — listing your calendars:\n")
    service = build("calendar", "v3", credentials=creds)
    calendars = service.calendarList().list().execute()

    for cal in calendars.get("items", []):
        access = cal.get("accessRole", "unknown")
        primary = " (PRIMARY)" if cal.get("primary") else ""
        print(f"  {cal['summary']}{primary} — {cal['id']} [{access}]")

    print(f"\nSetup complete. Found {len(calendars.get('items', []))} calendar(s).")
    print("You can now configure calendars.json with the calendar IDs above.")


if __name__ == "__main__":
    setup()
