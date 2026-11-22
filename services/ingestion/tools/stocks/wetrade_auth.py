"""E*TRADE authentication using browser automation.

Logs in to the E*TRADE website in a plain Google Chrome attached over CDP
(tools/chrome) to obtain the OAuth verifier, then exchanges it for API tokens.
Tokens are persisted so the browser is only needed once per day. The Chrome
profile is a trusted device, so unattended logins skip SMS; if E*TRADE asks for
SMS anyway, unattended runs raise (etrade_cli falls back to the PIN flow) and
attended runs (`etrade --headed`) prompt for the code in the terminal.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

import pyetrade

from ..chrome import chrome_cdp
from . import etrade_login
from .etrade_login import LoginRejected, StepUpRequired

logger = logging.getLogger("options-bot")

# Check if playwright is available
BROWSER_AUTH_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    BROWSER_AUTH_AVAILABLE = True
except ImportError:
    sync_playwright = None
    logger.debug("playwright not installed, browser-based auth unavailable")

# Token storage location
DEFAULT_TOKEN_FILE = Path.home() / ".etrade_tokens"


def _terminal_code_prompt() -> str:
    # Attended (--headed) runs only — unattended callers pass code_prompt=None.
    return input("E*TRADE sent an SMS verification code — enter it: ")


class BrowserAuth:
    """E*TRADE OAuth via a web login in Chrome over CDP (see module docstring)."""


    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        username: str,
        password: str,
        sandbox: bool = False,
        totp_secret: Optional[str] = None,
        token_file: Optional[Path] = None
    ):
        """Initialize browser-based authentication.
        
        Args:
            consumer_key: E*TRADE API consumer key
            consumer_secret: E*TRADE API consumer secret
            username: E*TRADE username
            password: E*TRADE password
            sandbox: If True, use sandbox environment
            totp_secret: TOTP secret for automatic 2FA (optional)
            token_file: Path to store/load access tokens
        """
        if not BROWSER_AUTH_AVAILABLE:
            raise ImportError("playwright not installed. Run: pip install playwright")
        
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.username = username
        self.password = password
        self.sandbox = sandbox
        self.totp_secret = totp_secret
        self.token_file = token_file or DEFAULT_TOKEN_FILE
        self.browser_profile = self.token_file.parent / ".etrade_chrome_profile"
        
        self._access_token = None
        self._access_token_secret = None
    
    def _load_tokens(self) -> bool:
        """Load saved access tokens from file.
        
        Returns:
            True if tokens were loaded successfully
        """
        if not self.token_file.exists():
            return False
        
        try:
            with open(self.token_file, 'r') as f:
                data = json.load(f)
            
            # Check if tokens are for the same mode (sandbox vs production)
            if data.get("sandbox") != self.sandbox:
                logger.info("Token mode mismatch, need fresh authentication")
                return False
            
            self._access_token = data.get("access_token")
            self._access_token_secret = data.get("access_token_secret")
            
            if self._access_token and self._access_token_secret:
                logger.info("Loaded saved access tokens")
                return True
                
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load tokens: {e}")
        
        return False
    
    def _save_tokens(self) -> None:
        """Save current access tokens to file."""
        if not self._access_token or not self._access_token_secret:
            return
        
        data = {
            "access_token": self._access_token,
            "access_token_secret": self._access_token_secret,
            "sandbox": self.sandbox,
        }
        
        with open(self.token_file, 'w') as f:
            json.dump(data, f)
        
        # Secure the file (readable only by owner)
        os.chmod(self.token_file, 0o600)
        logger.info(f"Saved access tokens to {self.token_file}")
    
    def _renew_access_token(self) -> bool:
        """Renew the current access token.
        
        E*TRADE access tokens expire at midnight US Eastern time.
        
        Returns:
            True if renewal succeeded
        """
        if not self._access_token or not self._access_token_secret:
            return False
        
        try:
            oauth = pyetrade.ETradeAccessManager(
                self.consumer_key,
                self.consumer_secret,
                self._access_token,
                self._access_token_secret
            )
            oauth.renew_access_token()
            logger.info("Access token renewed successfully")
            return True
        except Exception as e:
            # Expected daily occurrence (E*TRADE tokens expire at midnight ET);
            # authenticate() falls back to a fresh browser auth automatically.
            logger.info(f"Token renewal failed, will re-authenticate: {e}")
            return False
    
    def authenticate(
        self, headless: bool = False, login_timeout_ms: int = 120000, max_retries: int = 2
    ) -> Tuple[str, str]:
        """Perform authentication, using saved tokens if available.

        Args:
            headless: True = unattended (no terminal prompt; SMS step-up raises StepUpRequired)
            login_timeout_ms: How long to wait for the post-submit navigation
                (manual SMS entry needs the full default; an unattended
                caller that will fall back to the PIN-code flow on failure
                should pass a short value instead of waiting the full window
                for a login attempt no one is there to complete)
            max_retries: Login attempts before giving up. E*TRADE
                rejections and SMS step-ups are deterministic —
                retrying never turns a failure into a success, it only
                doubles the wait before an unattended caller's fallback
                kicks in, so pass 1 there.

        Returns:
            Tuple of (access_token, access_token_secret)
        """
        # Try to load and renew existing tokens first
        if self._load_tokens():
            if self._renew_access_token():
                logger.info("Using saved tokens (no browser needed)")
                return self._access_token, self._access_token_secret
            else:
                logger.info("Saved tokens expired, need fresh authentication")

        # Need browser-based login
        return self._browser_authenticate(headless, login_timeout_ms, max_retries)

    def _browser_authenticate(
        self, headless: bool = False, login_timeout_ms: int = 120000, max_retries: int = 2
    ) -> Tuple[str, str]:
        """Perform browser-based OAuth authentication.

        Args:
            headless: True = unattended (no terminal prompt; SMS step-up raises StepUpRequired)
            login_timeout_ms: passed through to _do_browser_login
            max_retries: passed through to _browser_login

        Returns:
            Tuple of (access_token, access_token_secret)
        """
        from authlib.integrations.requests_client import OAuth1Session
        
        logger.info("Starting browser-based authentication...")
        
        # Step 1: Get request token
        client = OAuth1Session(
            client_id=self.consumer_key,
            client_secret=self.consumer_secret,
            redirect_uri='oob'
        )
        
        request_token = client.fetch_request_token(
            url='https://api.etrade.com/oauth/request_token',
            params={'format': 'json'}
        )
        
        authorize_url = (
            f"https://us.etrade.com/e/t/etws/authorize?"
            f"key={self.consumer_key}&token={request_token['oauth_token']}"
        )
        
        # Step 2: Browser login to get verification code
        verification_code = self._browser_login(authorize_url, headless, login_timeout_ms, max_retries)
        
        if not verification_code:
            raise ValueError("Failed to get verification code from browser login")
        
        # Step 3: Exchange verification code for access token
        try:
            client.fetch_access_token(
                url='https://api.etrade.com/oauth/access_token',
                verifier=verification_code
            )
        except Exception as e:
            logger.error(f"Failed to get access token: {e}")
            raise
        
        self._access_token = client.token.get('oauth_token')
        self._access_token_secret = client.token.get('oauth_token_secret')
        
        # Save tokens for future use
        self._save_tokens()
        
        logger.info("Authentication successful!")
        return self._access_token, self._access_token_secret
    
    def _browser_login(
        self, authorize_url: str, headless: bool = False, login_timeout_ms: int = 120000, max_retries: int = 2
    ) -> Optional[str]:
        """Perform browser-based login and return verification code.

        Args:
            authorize_url: OAuth authorization URL
            headless: True = unattended (no terminal prompt; SMS step-up raises StepUpRequired)
            login_timeout_ms: How long to wait for the post-submit navigation
            max_retries: Login attempts before giving up

        Returns:
            Verification code string, or None if failed
        """
        for attempt in range(1, max_retries + 1):
            logger.info(f"Login attempt {attempt}/{max_retries}")
            try:
                code = self._do_browser_login(authorize_url, headless, login_timeout_ms)
                if code:
                    return code
            except (StepUpRequired, LoginRejected):
                raise  # deterministic — retrying only risks E*TRADE fraud flags
            except Exception as e:
                logger.warning(f"Login attempt {attempt} failed: {e}")
                if attempt >= max_retries:
                    logger.error(f"All {max_retries} login attempts failed")
                    raise
        return None
    
    def _do_browser_login(self, authorize_url: str, headless: bool, login_timeout_ms: int = 120000) -> Optional[str]:
        """Log in via Chrome over CDP and return the OAuth verifier.

        headless=True means unattended: never prompt; an SMS step-up raises
        StepUpRequired. Chrome itself always runs headed (Xvfb when no display).
        """
        code_prompt = None if headless else _terminal_code_prompt
        with sync_playwright() as p:
            with chrome_cdp(p, self.browser_profile) as context:
                page = context.new_page()
                etrade_login.login(
                    page, self.username, self.password,
                    totp_secret=self.totp_secret, code_prompt=code_prompt,
                    timeout_ms=login_timeout_ms,
                )
                return etrade_login.authorize(page, authorize_url)

    def revoke_tokens(self) -> None:
        """Revoke current access tokens and delete stored tokens."""
        if self._access_token and self._access_token_secret:
            try:
                oauth = pyetrade.ETradeAccessManager(
                    self.consumer_key,
                    self.consumer_secret,
                    self._access_token,
                    self._access_token_secret
                )
                oauth.revoke_access_token()
                logger.info("Access token revoked")
            except Exception as e:
                logger.warning(f"Failed to revoke token: {e}")
        
        # Clear stored tokens
        if self.token_file.exists():
            self.token_file.unlink()
            logger.info("Token file deleted")
        
        self._access_token = None
        self._access_token_secret = None
    
    @staticmethod
    def is_available() -> bool:
        """Check if browser auth is available."""
        return BROWSER_AUTH_AVAILABLE
    
    @staticmethod
    def has_credentials() -> bool:
        """Check if credentials are configured."""
        return bool(
            os.environ.get('ETRADE_USERNAME') and
            os.environ.get('ETRADE_PASSWORD')
        )


# Module-level constant
WETRADE_AVAILABLE = BROWSER_AUTH_AVAILABLE

# Alias for compatibility with main.py
WetradeAuth = BrowserAuth
