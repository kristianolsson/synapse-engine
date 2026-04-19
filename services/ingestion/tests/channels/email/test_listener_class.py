"""Tests for the EmailListener class."""

import unittest
from unittest.mock import MagicMock, patch
from services.ingestion.channels.email.listener import EmailListener
from services.ingestion import config

class TestEmailListenerClass(unittest.TestCase):
    def setUp(self):
        self.mock_rate_limiter = MagicMock()
        self.mock_rate_limiter.allow.return_value = True
        self.listener = EmailListener(rate_limiter=self.mock_rate_limiter)

        # Mock config values
        config.IMAP_HOST = "imap.example.com"
        config.IMAP_PORT = 993
        config.EMAIL_ADDRESS = "test@example.com"
        config.EMAIL_APP_PASSWORD = "password"

    @patch("services.ingestion.channels.email.listener.IMAPClient")
    def test_connect_success(self, mock_imap_cls):
        mock_client = MagicMock()
        mock_imap_cls.return_value = mock_client
        mock_client.list_folders.return_value = [
            (b"\\HasNoChildren", b"/", "INBOX"),
            (b"\\All \\HasNoChildren", b"/", "[Gmail]/All Mail"),
        ]

        self.listener._connect()

        mock_imap_cls.assert_called_with("imap.example.com", port=993, ssl=True)
        mock_client.login.assert_called_with("test@example.com", "password")
        mock_client.select_folder.assert_called_with("INBOX")
        self.assertEqual(self.listener._archive_folder, "[Gmail]/All Mail")

    @patch("services.ingestion.channels.email.listener.IMAPClient")
    @patch("services.ingestion.channels.email.listener.time")
    def test_reconnect_retry_logic(self, mock_time, mock_imap_cls):
        # First attempt raises exception, second succeeds
        mock_imap_cls.side_effect = [Exception("Connection failed"), MagicMock()]

        self.listener._reconnect()

        self.assertEqual(mock_imap_cls.call_count, 2)
        mock_time.sleep.assert_called_once()

    @patch("services.ingestion.channels.email.listener.process_email")
    @patch("services.ingestion.channels.email.listener.email.message_from_bytes")
    def test_fetch_and_process_success(self, mock_msg_from_bytes, mock_process):
        # Setup mocks
        mock_client = MagicMock()
        self.listener.client = mock_client
        self.listener._archive_folder = "Archive"

        # Mock fetch response
        uid = 123
        mock_client.fetch.return_value = {uid: {b"RFC822": b"raw_email_bytes"}}

        # Mock process_email to return (should_reply=False, ...)
        mock_process.return_value = (False, "", None)

        # Mock search to return empty list (stop loop)
        mock_client.search.return_value = []

        self.listener._fetch_and_process([uid])

        mock_client.fetch.assert_called_with([uid], ["RFC822"])
        mock_process.assert_called_once()
        mock_client.add_flags.assert_called_with([uid], [b"\\Seen"])
        mock_client.move.assert_called_with([uid], "Archive")

    @patch("services.ingestion.channels.email.reply.send_reply")
    @patch("services.ingestion.channels.email.listener.process_email")
    def test_fetch_and_process_reply(self, mock_process, mock_send_reply):
        mock_client = MagicMock()
        self.listener.client = mock_client
        self.listener._archive_folder = "Archive"

        uid = 123
        mock_client.fetch.return_value = {uid: {b"RFC822": b"raw_email_bytes"}}
        mock_process.return_value = (True, "Reply text", None)
        mock_client.search.return_value = []

        self.listener._fetch_and_process([uid])

        mock_send_reply.assert_called_once()

    @patch("services.ingestion.channels.email.listener.config")
    @patch("services.ingestion.channels.email.reply.send_reply")
    @patch("services.ingestion.channels.email.listener.process_email")
    def test_fetch_and_process_rate_limit_sends_reply(self, mock_process, mock_send_reply, mock_config):
        self.mock_rate_limiter.allow.return_value = False
        mock_config.REPLY_TO_ADDRESS = "reply@example.com"
        mock_client = MagicMock()
        self.listener.client = mock_client

        uid = 123
        mock_client.fetch.return_value = {uid: {b"RFC822": b"From: test@example.com\r\nSubject: Test\r\n\r\nHello"}}

        self.listener._fetch_and_process([uid])

        # Should fetch the message to reply, but NOT process it
        mock_client.fetch.assert_called_once()
        mock_process.assert_not_called()
        # Should send a rate limit reply
        mock_send_reply.assert_called_once()
        reply_body = mock_send_reply.call_args[1].get("body") or mock_send_reply.call_args[0][2]
        assert "Rate limit" in reply_body

    @patch("services.ingestion.channels.email.listener.IMAPClient")
    def test_run_loop_basics(self, mock_imap_cls):
        mock_client = MagicMock()
        mock_imap_cls.return_value = mock_client

        # Mock idle_check to return responses
        mock_client.idle_check.return_value = [(b"10", b"EXISTS")]

        # Mock search:
        # 1. First call is backlog check -> Return empty [] to enter loop
        # 2. Second call is inside loop -> Return [123] to trigger processing
        mock_client.search.side_effect = [[], [123]]

        # Hook into _fetch_and_process to stop the loop
        def stop_loop(uids):
            self.listener._running = False

        self.listener._fetch_and_process = MagicMock(side_effect=stop_loop)

        self.listener.run()

        mock_client.idle.assert_called()
        mock_client.idle_check.assert_called()
        mock_client.idle_done.assert_called()
        self.listener._fetch_and_process.assert_called_with([123])
