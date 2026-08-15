#!/usr/bin/env python3
"""
One-time setup script for Google OAuth2 authentication (Calendar + Gmail).

Usage:
    1. Download credentials.json from Google Cloud Console
       (APIs & Services > Credentials > OAuth 2.0 Client IDs > Download JSON)
    2. Enable both Google Calendar API and Gmail API in your Cloud project
    3. Place credentials.json in the synapse-engine root directory
    4. Run: python -m services.ingestion.shared.google_auth
    5. Complete the browser OAuth flow — grants Calendar + Gmail scopes at once
    6. token.json will be saved automatically and shared by both CLIs

Required Google Cloud setup:
    - Create a project in Google Cloud Console
    - Enable the Google Calendar API and Gmail API
    - Create OAuth 2.0 Client ID (Desktop application)
    - Add your email as a test user under OAuth consent screen → Test users
    - Download the credentials JSON
"""

import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
]

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_TOKEN_PATH = _ROOT / "token.json"
DEFAULT_CREDENTIALS_PATH = _ROOT / "credentials.json"


def setup(
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
) -> None:
    """Run the OAuth2 setup flow and verify Calendar + Gmail access."""

    if not credentials_path.exists():
        print(f"Error: credentials.json not found at {credentials_path}")
        print()
        print("To set up Google API access:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Enable the Google Calendar API and Gmail API")
        print("  3. Create an OAuth 2.0 Client ID (type: Desktop application)")
        print("  4. Download the JSON and save as: credentials.json")
        print(f"  5. Place it at: {credentials_path}")
        sys.exit(1)

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        # Invalidate token if it's missing any required scope
        if creds and creds.valid and creds.scopes and not set(SCOPES).issubset(creds.scopes):
            print("Existing token is missing required scopes — re-authorizing...")
            creds = None

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
        print("You will be asked to grant access to: Calendar + Gmail")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), SCOPES
        )
        creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        print(f"Token saved to {token_path}")

    # Verify Calendar access
    print("\nVerifying Calendar access — listing your calendars:\n")
    cal_service = build("calendar", "v3", credentials=creds)
    calendars = cal_service.calendarList().list().execute()
    for cal in calendars.get("items", []):
        access = cal.get("accessRole", "unknown")
        primary = " (PRIMARY)" if cal.get("primary") else ""
        print(f"  {cal['summary']}{primary} — {cal['id']} [{access}]")
    print(f"\nFound {len(calendars.get('items', []))} calendar(s).")

    # Verify Gmail access
    print("\nVerifying Gmail access — fetching profile:\n")
    gmail_service = build("gmail", "v1", credentials=creds)
    profile = gmail_service.users().getProfile(userId="me").execute()
    print(f"  Gmail address: {profile.get('emailAddress')}")
    print(f"  Total messages: {profile.get('messagesTotal', 'N/A')}")

    print("\nSetup complete. Both Calendar and Gmail are ready.")
    print("You can now configure calendars.json with the calendar IDs above.")


if __name__ == "__main__":
    setup()
