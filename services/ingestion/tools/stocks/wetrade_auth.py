"""E*TRADE authentication using browser automation.

This module provides browser-based authentication that handles SMS 2FA
by automating the login process while still allowing manual SMS code entry.
Includes token persistence so browser only opens once per day.
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

import pyetrade

logger = logging.getLogger("options-bot")

# Check if playwright is available
BROWSER_AUTH_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright
    BROWSER_AUTH_AVAILABLE = True
except ImportError:
    logger.debug("playwright not installed, browser-based auth unavailable")

# Token storage location
DEFAULT_TOKEN_FILE = Path.home() / ".etrade_tokens"


class BrowserAuth:
    """Authentication handler using Playwright for browser-based login.
    
    This handles SMS 2FA by opening a visible browser where you can
    enter the SMS code manually. Tokens are saved to disk and reused
    for subsequent runs on the same day.
    """


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
            raise ImportError(
                "playwright not installed. Run: pip install playwright && playwright install firefox"
            )
        
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.username = username
        self.password = password
        self.sandbox = sandbox
        self.totp_secret = totp_secret
        self.token_file = token_file or DEFAULT_TOKEN_FILE
        self.browser_profile = self.token_file.parent / ".etrade_browser_profile"
        
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
            headless: If True and TOTP configured, run headless (not for SMS)
            login_timeout_ms: How long to wait for the post-submit navigation
                (manual SMS entry needs the full default; an unattended
                caller that will fall back to the PIN-code flow on failure
                should pass a short value instead of waiting the full window
                for a login attempt no one is there to complete)
            max_retries: Login attempts before giving up. E*TRADE's fraud
                detection rejects automated submission deterministically —
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
            headless: If True and TOTP configured, run headless (not for SMS)
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
            headless: Run browser in headless mode (only for TOTP)
            login_timeout_ms: How long to wait for the post-submit navigation
            max_retries: Login attempts before giving up

        Returns:
            Verification code string, or None if failed
        """
        # If headless is explicitly requested, honor it.
        # This is critical for Docker environments where no display exists.
        use_headless = headless

        for attempt in range(1, max_retries + 1):
            logger.info(f"Login attempt {attempt}/{max_retries}")

            try:
                code = self._do_browser_login(authorize_url, use_headless, login_timeout_ms)
                if code:
                    return code
            except Exception as e:
                logger.warning(f"Login attempt {attempt} failed: {e}")
                if attempt >= max_retries:
                    logger.error(f"All {max_retries} login attempts failed")
                    raise
        
        return None
    
    def _do_browser_login(self, authorize_url: str, headless: bool, login_timeout_ms: int = 120000) -> Optional[str]:
        """Execute the browser login flow."""
        with sync_playwright() as p:
            logger.info("Opening Firefox browser with persistent profile...")
            context = p.firefox.launch_persistent_context(
                user_data_dir=str(self.browser_profile),
                headless=headless,
                viewport={'width': 1280, 'height': 800}
            )

            try:
                page = context.pages[0] if context.pages else context.new_page()

                # Navigate to E*TRADE login
                logger.info("Navigating to E*TRADE login page...")
                page.goto('https://us.etrade.com/etx/pxy/login', timeout=30000)

                # Fill in credentials
                logger.info("Entering credentials...")
                page.locator('#USER').fill(self.username)
                page.locator('#password').fill(self.password)

                # Handle 2FA
                if self.totp_secret:
                    # Use TOTP
                    from pyotp import TOTP
                    totp = TOTP(self.totp_secret)
                    page.locator('[for="useSecurityCode"]').click()
                    page.locator('#securityCode').fill(totp.now())
                else:
                    # SMS 2FA - user will enter code manually
                    logger.info("SMS 2FA - please enter your code in the browser when prompted")

                # Click login
                page.locator('#mfaLogonButton').click()
                
                # Wait for login to complete (give user time for SMS)
                logger.info(f"Waiting for login to complete (up to {login_timeout_ms / 1000:.0f} seconds)...")
                page.wait_for_url(
                    lambda url: 'etrade.com/etx/pxy/login' not in url,
                    timeout=login_timeout_ms
                )
                
                # Handle any intermediate pages (like "Continue" prompts)
                try:
                    continue_btn = page.locator('button:has-text("Continue")')
                    if continue_btn.is_visible(timeout=3000):
                        continue_btn.click()
                        page.wait_for_timeout(2000)
                except Exception:
                    pass  # No continue button, that's fine
                
                # Now navigate to the OAuth authorization page
                logger.info("Navigating to OAuth authorization...")
                page.goto(authorize_url, wait_until='domcontentloaded', timeout=30000)
                
                # Wait for and click Accept button (try multiple selectors)
                logger.info("Looking for Accept button...")
                accept_clicked = False
                
                # Try different selectors for the Accept button
                selectors = [
                    '[value="Accept"]',
                    'input[value="Accept"]',
                    'button:has-text("Accept")',
                    '#acceptSubmit',
                    'input[type="submit"][value="Accept"]'
                ]
                
                for selector in selectors:
                    try:
                        accept_btn = page.locator(selector)
                        if accept_btn.is_visible(timeout=5000):
                            accept_btn.click(timeout=10000)
                            accept_clicked = True
                            logger.info(f"Clicked Accept button with selector: {selector}")
                            break
                    except Exception:
                        continue
                
                if not accept_clicked:
                    # Maybe the page shows the code directly without needing to click
                    logger.warning("Could not find Accept button, checking for verification code...")
                
                # Wait for redirect and extract verification code
                page.wait_for_timeout(3000)
                
                # Try to get the verification code from the page
                code = None
                
                # Method 1: Input field
                try:
                    code_input = page.locator('input[type="text"]')
                    if code_input.is_visible(timeout=3000):
                        code = code_input.get_attribute('value')
                        if code:
                            code = code.strip()
                except Exception:
                    pass
                
                # Method 2: Look for code in page text
                if not code:
                    try:
                        # Sometimes the code is displayed as text
                        page_text = page.content()
                        import re
                        # Look for a verification code pattern (usually alphanumeric, 5-10 chars)
                        matches = re.findall(r'code["\s:]+([A-Z0-9]{5,10})', page_text, re.IGNORECASE)
                        if matches:
                            code = matches[0]
                    except Exception:
                        pass
                
                if code:
                    logger.info(f"Got verification code: {code[:3]}...")
                    return code
                else:
                    logger.error("Could not extract verification code from page")
                    # Take a screenshot for debugging
                    try:
                        page.screenshot(path='/tmp/etrade_auth_debug.png')
                        logger.info("Screenshot saved to /tmp/etrade_auth_debug.png")
                    except Exception:
                        pass
                    return None
                    
            finally:
                context.close()
    
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
