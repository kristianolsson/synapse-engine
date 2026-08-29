"""Tests for E*TRADE OAuth authentication (tools/stocks/auth.py's ETradeAuth).

ETradeAuth is the fallback auth path whenever WetradeAuth isn't configured
(no wetrade username/password, or the wetrade dependency unavailable) — not
dead code, just unused in this deployment's current config. These tests
lock in that its headless mode never blocks on input().
"""

from unittest.mock import MagicMock, patch

import pytest

from services.ingestion.tools.stocks.auth import ETradeAuth


@pytest.fixture
def auth(tmp_path):
    return ETradeAuth(
        consumer_key="key",
        consumer_secret="secret",
        sandbox=True,
        token_file=tmp_path / ".etrade_tokens_test",
    )


class TestAuthenticateHeadless:
    @patch("services.ingestion.tools.stocks.auth.pyetrade")
    def test_raises_instead_of_blocking_on_input(self, mock_pyetrade, auth):
        mock_oauth = MagicMock()
        mock_oauth.get_request_token.return_value = "https://example.com/authorize"
        mock_pyetrade.ETradeOAuth.return_value = mock_oauth

        with patch("builtins.input") as mock_input, \
             patch("services.ingestion.tools.stocks.auth.webbrowser.open") as mock_open:
            with pytest.raises(RuntimeError, match="Headless"):
                auth.authenticate(headless=True)

        mock_input.assert_not_called()
        mock_open.assert_not_called()

    @patch("services.ingestion.tools.stocks.auth.pyetrade")
    def test_raised_exception_is_a_plain_exception_callers_can_catch(self, mock_pyetrade, auth):
        # etrade_cli.py's _authenticate() wraps this call in a bare
        # `except Exception` that routes to the PIN-auth Telegram/email
        # fallback — this must stay a normal, catchable exception.
        mock_oauth = MagicMock()
        mock_oauth.get_request_token.return_value = "https://example.com/authorize"
        mock_pyetrade.ETradeOAuth.return_value = mock_oauth

        with patch("builtins.input"), \
             patch("services.ingestion.tools.stocks.auth.webbrowser.open"):
            try:
                auth.authenticate(headless=True)
                pytest.fail("expected authenticate(headless=True) to raise")
            except Exception as e:
                assert isinstance(e, RuntimeError)


class TestAuthenticateInteractive:
    @patch("services.ingestion.tools.stocks.auth.pyetrade")
    def test_non_headless_still_prompts_for_verification_code(self, mock_pyetrade, auth):
        mock_oauth = MagicMock()
        mock_oauth.get_request_token.return_value = "https://example.com/authorize"
        mock_oauth.get_access_token.return_value = {
            "oauth_token": "tok-123",
            "oauth_token_secret": "sec-456",
        }
        mock_pyetrade.ETradeOAuth.return_value = mock_oauth

        with patch("services.ingestion.tools.stocks.auth.webbrowser.open") as mock_open, \
             patch("builtins.input", return_value="  123456  ") as mock_input:
            access_token, access_token_secret = auth.authenticate(headless=False)

        mock_open.assert_called_once_with("https://example.com/authorize")
        mock_input.assert_called_once()
        mock_oauth.get_access_token.assert_called_once_with("123456")
        assert access_token == "tok-123"
        assert access_token_secret == "sec-456"
        assert auth.token_file.exists()
