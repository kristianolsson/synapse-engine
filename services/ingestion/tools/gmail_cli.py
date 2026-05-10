#!/usr/bin/env python3
"""
Gmail CLI tool for synapse-engine.

Provides subcommands to read inbox, manage labels, and create drafts.
Shares token.json and credentials.json with calendar_cli — one auth covers both.

Usage:
    python gmail_cli.py list-inbox [--limit N] [--query Q]
    python gmail_cli.py search <query> [--limit N]
    python gmail_cli.py read-thread <thread-id>
    python gmail_cli.py list-labels
    python gmail_cli.py apply-label <thread-id> <label>
    python gmail_cli.py remove-label <thread-id> <label>
    python gmail_cli.py create-label <name>
    python gmail_cli.py create-draft --to <email> --subject <subject> --body <body>
    python gmail_cli.py reply-draft <thread-id> --body <body>
    python gmail_cli.py list-drafts [--limit N]
    python gmail_cli.py read-draft <draft-id>

Gated commands (disabled unless env flag is set):
    GMAIL_ALLOW_ARCHIVE=true  →  archive-thread <thread-id>
    GMAIL_ALLOW_SEND=true     →  send-draft <draft-id>
"""

import argparse
import base64
import fcntl
import os
import sys
from email.mime.text import MIMEText
from io import StringIO
from pathlib import Path
from typing import Optional

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

_ALLOW_ARCHIVE = os.getenv("GMAIL_ALLOW_ARCHIVE", "").lower() in ("1", "true", "yes")
_ALLOW_SEND = os.getenv("GMAIL_ALLOW_SEND", "").lower() in ("1", "true", "yes")


def get_credentials(token_path: Path, credentials_path: Path) -> Credentials:
    """Load or refresh OAuth2 credentials, serialized via a lockfile shared with calendar_cli."""
    lock_path = token_path.with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            creds = None
            if token_path.exists():
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                # Force re-auth if the token is missing any required scope
                if creds and creds.valid and creds.scopes and not set(SCOPES).issubset(creds.scopes):
                    creds = None

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

                with open(token_path, "w") as f:
                    f.write(creds.to_json())

            return creds
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def build_service(token_path: Path, credentials_path: Path):
    """Build a Gmail API service client."""
    creds = get_credentials(token_path, credentials_path)
    return build("gmail", "v1", credentials=creds)


def _resolve_label_id(service, label_name: str) -> str:
    """Resolve a label name to its ID (case-insensitive). Raises ValueError if not found."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"].lower() == label_name.lower():
            return lbl["id"]
    available = ", ".join(sorted(lbl["name"] for lbl in labels))
    raise ValueError(f"Label '{label_name}' not found. Available: {available}")


def _get_header(headers: list, name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text body from a message payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _extract_body(part)
            if text:
                return text
    return ""


# --- Commands -----------------------------------------------------------------------


def _list_threads(service, limit: int, query: str = "", label_ids: list = None) -> str:
    """Shared implementation for listing threads, with optional label and query filters."""
    out = StringIO()
    params: dict = {"userId": "me", "maxResults": limit}
    if label_ids:
        params["labelIds"] = label_ids
    if query:
        params["q"] = query

    result = service.users().threads().list(**params).execute()
    threads = result.get("threads", [])

    if not threads:
        out.write("No threads found.\n")
        return out.getvalue()

    for t in threads:
        thread = service.users().threads().get(
            userId="me", id=t["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        messages = thread.get("messages", [])
        if not messages:
            continue
        hdrs = messages[0].get("payload", {}).get("headers", [])
        subject = _get_header(hdrs, "Subject") or "(no subject)"
        sender = _get_header(hdrs, "From")
        date = _get_header(hdrs, "Date")
        snippet = t.get("snippet", "")
        count_str = f" [{len(messages)}]" if len(messages) > 1 else ""

        out.write(f"Thread: {t['id']}{count_str}\n")
        out.write(f"  From:    {sender}\n")
        out.write(f"  Date:    {date}\n")
        out.write(f"  Subject: {subject}\n")
        out.write(f"  Snippet: {snippet[:120]}\n\n")

    return out.getvalue()


def cmd_list_inbox(service, limit: int = 20, query: str = "") -> str:
    return _list_threads(service, limit=limit, query=query, label_ids=["INBOX"])


def cmd_search(service, query: str, limit: int = 20) -> str:
    return _list_threads(service, limit=limit, query=query)


def cmd_read_thread(service, thread_id: str) -> str:
    out = StringIO()
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()
    messages = thread.get("messages", [])

    if not messages:
        out.write("Thread not found or empty.\n")
        return out.getvalue()

    out.write(f"Thread ID: {thread_id} ({len(messages)} message(s))\n")
    out.write("=" * 60 + "\n\n")

    for i, msg in enumerate(messages, 1):
        payload = msg.get("payload", {})
        hdrs = payload.get("headers", [])
        out.write(f"--- Message {i} ---\n")
        out.write(f"From:    {_get_header(hdrs, 'From')}\n")
        out.write(f"Date:    {_get_header(hdrs, 'Date')}\n")
        subject = _get_header(hdrs, "Subject")
        if subject:
            out.write(f"Subject: {subject}\n")
        labels = msg.get("labelIds", [])
        if labels:
            out.write(f"Labels:  {', '.join(labels)}\n")
        out.write("\n")
        body = _extract_body(payload)
        out.write(body.strip() if body else "(no text content)")
        out.write("\n\n")

    return out.getvalue()


def cmd_list_labels(service) -> str:
    out = StringIO()
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    system = sorted([l for l in labels if l.get("type") == "system"], key=lambda l: l["name"])
    user = sorted([l for l in labels if l.get("type") != "system"], key=lambda l: l["name"])

    out.write("System labels:\n")
    for lbl in system:
        out.write(f"  {lbl['name']} (id: {lbl['id']})\n")
    if user:
        out.write("\nUser labels:\n")
        for lbl in user:
            out.write(f"  {lbl['name']} (id: {lbl['id']})\n")
    return out.getvalue()


def cmd_apply_label(service, thread_id: str, label_name: str) -> str:
    label_id = _resolve_label_id(service, label_name)
    service.users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": [label_id]}
    ).execute()
    return f"Label '{label_name}' applied to thread {thread_id}."


def cmd_remove_label(service, thread_id: str, label_name: str) -> str:
    label_id = _resolve_label_id(service, label_name)
    service.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": [label_id]}
    ).execute()
    return f"Label '{label_name}' removed from thread {thread_id}."


def cmd_create_label(service, name: str) -> str:
    label = service.users().labels().create(
        userId="me", body={"name": name}
    ).execute()
    return f"Label created: '{label['name']}' (id: {label['id']})."


def cmd_create_draft(service, to: str, subject: str, body: str) -> str:
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    return f"Draft created (id: {draft['id']})."


def cmd_reply_draft(service, thread_id: str, body: str) -> str:
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["Subject", "From", "Message-ID"],
    ).execute()
    messages = thread.get("messages", [])
    if not messages:
        raise ValueError(f"Thread '{thread_id}' not found or has no messages.")

    last = messages[-1]
    hdrs = last.get("payload", {}).get("headers", [])
    subject = _get_header(hdrs, "Subject")
    from_addr = _get_header(hdrs, "From")
    message_id = _get_header(hdrs, "Message-ID")

    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg = MIMEText(body)
    msg["to"] = from_addr
    msg["subject"] = reply_subject
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": thread_id}},
    ).execute()
    return f"Reply draft created (id: {draft['id']}) in thread {thread_id}."


def cmd_list_drafts(service, limit: int = 20) -> str:
    out = StringIO()
    result = service.users().drafts().list(userId="me", maxResults=limit).execute()
    drafts = result.get("drafts", [])

    if not drafts:
        out.write("No drafts found.\n")
        return out.getvalue()

    for d in drafts:
        draft = service.users().drafts().get(
            userId="me", id=d["id"], format="metadata",
            metadataHeaders=["Subject", "To", "Date"],
        ).execute()
        msg = draft.get("message", {})
        hdrs = msg.get("payload", {}).get("headers", [])
        subject = _get_header(hdrs, "Subject") or "(no subject)"
        to = _get_header(hdrs, "To")
        date = _get_header(hdrs, "Date")
        snippet = msg.get("snippet", "")

        out.write(f"Draft: {d['id']}\n")
        out.write(f"  To:      {to}\n")
        out.write(f"  Date:    {date}\n")
        out.write(f"  Subject: {subject}\n")
        out.write(f"  Snippet: {snippet[:120]}\n\n")

    return out.getvalue()


def cmd_read_draft(service, draft_id: str) -> str:
    out = StringIO()
    draft = service.users().drafts().get(
        userId="me", id=draft_id, format="full"
    ).execute()
    msg = draft.get("message", {})
    payload = msg.get("payload", {})
    hdrs = payload.get("headers", [])

    out.write(f"Draft ID: {draft_id}\n")
    out.write(f"To:      {_get_header(hdrs, 'To')}\n")
    out.write(f"Date:    {_get_header(hdrs, 'Date')}\n")
    subject = _get_header(hdrs, "Subject")
    if subject:
        out.write(f"Subject: {subject}\n")
    thread_id = msg.get("threadId")
    if thread_id:
        out.write(f"Thread:  {thread_id}\n")
    out.write("\n")
    body = _extract_body(payload)
    out.write(body.strip() if body else "(no text content)")
    out.write("\n")

    return out.getvalue()


def cmd_archive_thread(service, thread_id: str) -> str:
    service.users().threads().modify(
        userId="me", id=thread_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()
    return f"Thread {thread_id} archived (removed from inbox)."


def cmd_send_draft(service, draft_id: str) -> str:
    sent = service.users().drafts().send(
        userId="me", body={"id": draft_id}
    ).execute()
    return f"Draft {draft_id} sent (message id: {sent.get('id', 'unknown')})."


# --- CLI entry point ----------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="gmail",
        description="Gmail CLI for synapse-engine",
        add_help=False,
    )
    parser.add_argument("--help", action="help", help=argparse.SUPPRESS)
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS_PATH, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-inbox", help="List inbox threads")
    p.add_argument("--limit", type=int, default=20, help="Max threads to return (default: 20)")
    p.add_argument("--query", default="", help="Gmail search query (e.g. 'is:unread', 'from:someone@example.com')")

    p = sub.add_parser("search", help="Search all mail (not limited to inbox)")
    p.add_argument("query", help="Gmail search query (e.g. 'from:boss@example.com is:unread')")
    p.add_argument("--limit", type=int, default=20, help="Max threads to return (default: 20)")

    p = sub.add_parser("read-thread", help="Read all messages in a thread")
    p.add_argument("thread_id", help="Thread ID (from list-inbox or search output)")

    sub.add_parser("list-labels", help="List all Gmail labels")

    p = sub.add_parser("apply-label", help="Apply a label to a thread")
    p.add_argument("thread_id", help="Thread ID")
    p.add_argument("label", help="Label name (case-insensitive)")

    p = sub.add_parser("remove-label", help="Remove a label from a thread")
    p.add_argument("thread_id", help="Thread ID")
    p.add_argument("label", help="Label name (case-insensitive)")

    p = sub.add_parser("create-label", help="Create a new label")
    p.add_argument("name", help="Label name")

    p = sub.add_parser("create-draft", help="Create a new draft email")
    p.add_argument("--to", required=True, help="Recipient email address")
    p.add_argument("--subject", required=True, help="Email subject")
    p.add_argument("--body", required=True, help="Email body text")

    p = sub.add_parser("reply-draft", help="Create a draft reply in a thread")
    p.add_argument("thread_id", help="Thread ID to reply to")
    p.add_argument("--body", required=True, help="Reply body text")

    p = sub.add_parser("list-drafts", help="List draft emails")
    p.add_argument("--limit", type=int, default=20, help="Max drafts to return (default: 20)")

    p = sub.add_parser("read-draft", help="Read the full content of a draft")
    p.add_argument("draft_id", help="Draft ID (from list-drafts output)")

    # Gated: only registered (and visible in --help) when env flag is set
    if _ALLOW_ARCHIVE:
        p = sub.add_parser("archive-thread", help="Remove a thread from inbox")
        p.add_argument("thread_id", help="Thread ID")

    if _ALLOW_SEND:
        p = sub.add_parser("send-draft", help="Send a draft email")
        p.add_argument("draft_id", help="Draft ID (from create-draft output)")

    args = parser.parse_args(argv)

    try:
        service = build_service(args.token, args.credentials)

        if args.command == "list-inbox":
            result = cmd_list_inbox(service, limit=args.limit, query=args.query)
        elif args.command == "search":
            result = cmd_search(service, query=args.query, limit=args.limit)
        elif args.command == "read-thread":
            result = cmd_read_thread(service, args.thread_id)
        elif args.command == "list-labels":
            result = cmd_list_labels(service)
        elif args.command == "apply-label":
            result = cmd_apply_label(service, args.thread_id, args.label)
        elif args.command == "remove-label":
            result = cmd_remove_label(service, args.thread_id, args.label)
        elif args.command == "create-label":
            result = cmd_create_label(service, args.name)
        elif args.command == "create-draft":
            result = cmd_create_draft(service, to=args.to, subject=args.subject, body=args.body)
        elif args.command == "reply-draft":
            result = cmd_reply_draft(service, thread_id=args.thread_id, body=args.body)
        elif args.command == "list-drafts":
            result = cmd_list_drafts(service, limit=args.limit)
        elif args.command == "read-draft":
            result = cmd_read_draft(service, args.draft_id)
        elif args.command == "archive-thread":
            result = cmd_archive_thread(service, args.thread_id)
        elif args.command == "send-draft":
            result = cmd_send_draft(service, args.draft_id)
        else:
            return

        print(result)
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
