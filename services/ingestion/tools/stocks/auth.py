"""E*TRADE OAuth authentication handler.

Handles the OAuth 1.0a three-legged authentication flow for E*TRADE API.
Supports token persistence for automated daily runs.
"""

import os
import json
import webbrowser
import logging
from pathlib import Path
from typing import Optional, Tuple

import pyetrade

logger = logging.getLogger("options-bot")

# Token storage location
DEFAULT_TOKEN_FILE = Path.home() / ".etrade_tokens"


class ETradeAuth:
    """Handles E*TRADE OAuth authentication."""
    
    # E*TRADE API base URLs
    SANDBOX_BASE_URL = "https://apisb.etrade.com"
    PRODUCTION_BASE_URL = "https://api.etrade.com"
    
    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        sandbox: bool = True,
        token_file: Optional[Path] = None
    ):
        """Initialize the authentication handler.
        
        Args:
            consumer_key: E*TRADE API consumer key
            consumer_secret: E*TRADE API consumer secret
            sandbox: If True, use sandbox environment; else production
            token_file: Path to store/load access tokens
        """
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.sandbox = sandbox
        self.token_file = token_file or DEFAULT_TOKEN_FILE
        
        self._access_token: Optional[str] = None
        self._access_token_secret: Optional[str] = None
        self._oauth = None
        
    @property
    def base_url(self) -> str:
        """Get the appropriate base URL for the current mode."""
        return self.SANDBOX_BASE_URL if self.sandbox else self.PRODUCTION_BASE_URL
    
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
    
    def authenticate(self, headless: bool = False) -> Tuple[str, str]:
        """Perform OAuth authentication flow.

        Args:
            headless: If True, raise instead of blocking on input() — see below

        Returns:
            Tuple of (access_token, access_token_secret)

        Raises:
            Exception: If authentication fails, or immediately if headless
                (see below).
        """
        # Try to load existing tokens first
        if self._load_tokens():
            # Verify tokens are still valid by trying to renew
            try:
                self._renew_access_token()
                return self._access_token, self._access_token_secret
            except Exception as e:
                logger.warning(f"Token renewal failed, performing fresh auth: {e}")

        # Perform fresh OAuth flow
        logger.info("Starting OAuth authentication flow...")

        # Step 1: Get request token and auth URL
        # Note: pyetrade.ETradeOAuth.get_request_token() returns only the auth URL
        # The oauth object stores the request token internally
        oauth = pyetrade.ETradeOAuth(self.consumer_key, self.consumer_secret)
        auth_url = oauth.get_request_token()

        logger.info("Authorization URL generated")

        # Step 2: User authorization
        if headless:
            # No one is present to paste back a verification code — raise
            # instead of blocking on input() below (which would otherwise
            # hang an unattended run indefinitely). etrade_cli.py's
            # _authenticate() catches this and routes to the PIN-auth
            # Telegram/email fallback, the same way a WetradeAuth failure
            # already does.
            raise RuntimeError(
                "Headless E*TRADE auth requires a human to complete the "
                "OAuth flow interactively; falling back to PIN-auth."
            )
        print("\nOpening browser for E*TRADE authorization...")
        webbrowser.open(auth_url)

        # Get verification code from user
        verifier = input("\nEnter the verification code from E*TRADE: ").strip()
        
        # Step 3: Get access token using the same oauth object
        tokens = oauth.get_access_token(verifier)
        self._access_token = tokens['oauth_token']
        self._access_token_secret = tokens['oauth_token_secret']
        
        # Save tokens for future use
        self._save_tokens()
        
        logger.info("Authentication successful!")
        return self._access_token, self._access_token_secret
    
    def _renew_access_token(self) -> None:
        """Renew the current access token.
        
        E*TRADE access tokens expire at midnight US Eastern time.
        This attempts to renew the token.
        """
        if not self._access_token or not self._access_token_secret:
            raise ValueError("No access tokens to renew")
        
        oauth = pyetrade.ETradeAccessManager(
            self.consumer_key,
            self.consumer_secret,
            self._access_token,
            self._access_token_secret
        )
        
        # Try to renew
        oauth.renew_access_token()
        logger.info("Access token renewed successfully")
    
    def get_credentials(self) -> Tuple[str, str, str, str]:
        """Get all credentials needed for API calls.
        
        Returns:
            Tuple of (consumer_key, consumer_secret, access_token, access_token_secret)
        """
        if not self._access_token or not self._access_token_secret:
            self.authenticate()
        
        return (
            self.consumer_key,
            self.consumer_secret,
            self._access_token,
            self._access_token_secret
        )
    
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
