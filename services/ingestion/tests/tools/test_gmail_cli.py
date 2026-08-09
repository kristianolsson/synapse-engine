"""
Unit tests for the Gmail CLI tool's body-extraction logic.

Covers the HTML-only-body fallback: gmail_cli previously returned "(no text
content)" for messages with no text/plain MIME part, even when a text/html
part existed. All tests mock the Google API modules — no real API calls.
"""

import base64
import sys
from unittest import mock

import pytest


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8").rstrip("=")


@pytest.fixture
def gmail_cli():
    """Import gmail_cli with the google API modules mocked out."""
    patches = {
        "google.oauth2.credentials": mock.MagicMock(),
        "google.oauth2": mock.MagicMock(),
        "google.auth.transport.requests": mock.MagicMock(),
        "google.auth.transport": mock.MagicMock(),
        "google.auth": mock.MagicMock(),
        "google": mock.MagicMock(),
        "google_auth_oauthlib.flow": mock.MagicMock(),
        "google_auth_oauthlib": mock.MagicMock(),
        "googleapiclient.discovery": mock.MagicMock(),
        "googleapiclient": mock.MagicMock(),
    }
    with mock.patch.dict(sys.modules, patches):
        if "services.ingestion.tools.gmail_cli" in sys.modules:
            del sys.modules["services.ingestion.tools.gmail_cli"]
        from services.ingestion.tools import gmail_cli as module

        yield module


class TestExtractBodyPlainText:
    def test_single_part_plain_text(self, gmail_cli):
        payload = {"mimeType": "text/plain", "body": {"data": _b64("Hello plain")}}
        assert gmail_cli._extract_body(payload) == "Hello plain"

    def test_plain_text_preferred_over_html(self, gmail_cli):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Hello plain")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>html version</p>")}},
            ],
        }
        assert gmail_cli._extract_body(payload) == "Hello plain"

    def test_no_body_parts_returns_empty(self, gmail_cli):
        payload = {"mimeType": "multipart/mixed", "parts": []}
        assert gmail_cli._extract_body(payload) == ""


class TestExtractBodyHtmlFallback:
    """The core regression: HTML-only messages must not read as empty."""

    def test_html_only_single_part(self, gmail_cli):
        payload = {"mimeType": "text/html", "body": {"data": _b64("<p>Hello html</p>")}}
        result = gmail_cli._extract_body(payload)
        assert result != ""
        assert "(no text content)" not in result
        assert "Hello html" in result

    def test_html_only_multipart_alternative(self, gmail_cli):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<p>Bostadsformedlingen fee notice</p>")}},
            ],
        }
        result = gmail_cli._extract_body(payload)
        assert "Bostadsformedlingen fee notice" in result

    def test_html_fallback_is_labeled(self, gmail_cli):
        payload = {"mimeType": "text/html", "body": {"data": _b64("<p>Hello html</p>")}}
        result = gmail_cli._extract_body(payload)
        assert result.startswith("[Body extracted from HTML]")

    def test_plain_text_not_labeled(self, gmail_cli):
        payload = {"mimeType": "text/plain", "body": {"data": _b64("Hello plain")}}
        result = gmail_cli._extract_body(payload)
        assert "[Body extracted from HTML]" not in result

    def test_html_entities_decoded(self, gmail_cli):
        payload = {"mimeType": "text/html", "body": {"data": _b64("<p>Din avgift &auml;r 250 kr</p>")}}
        result = gmail_cli._extract_body(payload)
        assert "Din avgift är 250 kr" in result

    def test_html_in_related_part_within_mixed(self, gmail_cli):
        """multipart/mixed > multipart/related > text/html (inline images case)."""
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/related",
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": _b64("<p>Nested html</p>")}},
                    ],
                },
            ],
        }
        result = gmail_cli._extract_body(payload)
        assert "Nested html" in result


class TestHtmlToText:
    def test_strips_style_and_script(self, gmail_cli):
        html_body = "<style>p{color:red}</style><script>track()</script><p>Visible text</p>"
        result = gmail_cli._html_to_text(html_body)
        assert "Visible text" in result
        assert "color" not in result
        assert "track()" not in result

    def test_preserves_link_url(self, gmail_cli):
        html_body = '<p>Betala via <a href="https://example.com/pay?id=123">denna länk</a>.</p>'
        result = gmail_cli._html_to_text(html_body)
        assert "denna länk" in result
        assert "https://example.com/pay?id=123" in result

    def test_preserves_headings(self, gmail_cli):
        result = gmail_cli._html_to_text("<h2>Din betalning</h2><p>text</p>")
        assert "## Din betalning" in result

    def test_preserves_list_items(self, gmail_cli):
        result = gmail_cli._html_to_text("<ul><li>Item one</li><li>Item two</li></ul>")
        assert "- Item one" in result
        assert "- Item two" in result

    def test_preserves_table_rows(self, gmail_cli):
        result = gmail_cli._html_to_text("<table><tr><th>Post</th><th>Belopp</th></tr></table>")
        assert "| Post | Belopp |" in result

    def test_empty_html_returns_empty(self, gmail_cli):
        assert gmail_cli._html_to_text("<html><body></body></html>") == ""


class TestReadThreadRendersHtmlBody:
    """End-to-end through cmd_read_thread with a mocked Gmail service."""

    def test_html_only_message_not_reported_as_no_text_content(self, gmail_cli):
        service = mock.MagicMock()
        service.users().threads().get().execute.return_value = {
            "messages": [
                {
                    "id": "msg1",
                    "labelIds": ["INBOX"],
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "queue@bostadsformedlingen.se"},
                            {"name": "Date", "value": "Mon, 1 Jan 2026 10:00:00 +0000"},
                            {"name": "Subject", "value": "Avgift"},
                        ],
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>Din k&ouml;avgift &auml;r 250 kr.</p>")},
                    },
                }
            ]
        }
        result = gmail_cli.cmd_read_thread(service, "thread123")
        assert "(no text content)" not in result
        assert "Din köavgift är 250 kr" in result
