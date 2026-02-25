"""Tests for the TelegramListener class."""

import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from services.ingestion.channels.telegram.listener import TelegramListener
from services.ingestion import config

class TestTelegramListenerClass(unittest.TestCase):
    def setUp(self):
        self.mock_rate_limiter = MagicMock()
        self.listener = TelegramListener(rate_limiter=self.mock_rate_limiter)
        config.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    @patch("services.ingestion.channels.telegram.listener.Application")
    def test_run_success(self, mock_app_cls):
        # Mock the builder chain: Application.builder().token().build()
        mock_builder = MagicMock()
        mock_app_cls.builder.return_value = mock_builder
        mock_builder.token.return_value = mock_builder

        mock_app = MagicMock()
        mock_builder.build.return_value = mock_app

        self.listener.run()

        # Verification
        mock_app_cls.builder.assert_called_once()
        mock_builder.token.assert_called_with("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
        mock_builder.build.assert_called_once()

        # Check if handler was added
        mock_app.add_handler.assert_called_once()

        # Check if polling started
        mock_app.run_polling.assert_called_once()
        args, kwargs = mock_app.run_polling.call_args
        self.assertEqual(kwargs['drop_pending_updates'], True)
        self.assertEqual(kwargs['stop_signals'], [])

    def test_run_no_token(self):
        config.TELEGRAM_BOT_TOKEN = ""
        with patch("services.ingestion.channels.telegram.listener.Application") as mock_app_cls:
            self.listener.run()
            mock_app_cls.assert_not_called()

    @patch("services.ingestion.channels.telegram.listener.asyncio")
    def test_stop_calls_app_stop(self, mock_asyncio):
        mock_app = MagicMock()
        mock_app.running = True
        self.listener._app = mock_app

        mock_loop = MagicMock()
        mock_asyncio.get_event_loop.return_value = mock_loop

        self.listener.stop()

        mock_loop.call_soon_threadsafe.assert_called_with(mock_app.stop)

    @patch("services.ingestion.channels.telegram.listener.asyncio")
    def test_stop_ignored_if_not_running(self, mock_asyncio):
        mock_app = MagicMock()
        mock_app.running = False
        self.listener._app = mock_app

        self.listener.stop()

        mock_asyncio.get_event_loop.assert_not_called()
