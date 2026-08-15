"""Browser lifecycle for Amazon Fresh CLI.

Handles Firefox persistent context with profile reuse (same pattern as E*TRADE wetrade_auth).
Profile directory: ~/.amazon-fresh-session/
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("amazon-fresh")

DEFAULT_PROFILE_DIR = Path.home() / ".amazon-fresh-session"

# All Amazon auth/2FA/challenge URL patterns — polling waits until NONE of these match
AUTH_REDIRECT_PATTERNS = [
    "/ap/signin",
    "/ap/register",
    "/gp/sign-in",
    "/ap/mfa",           # SMS / TOTP 2FA page
    "/ap/cvf",           # Email/phone verification
    "/ap/challenge",     # Bot/CAPTCHA challenge
    "/ap/verify",        # Additional verification
    "/ap/password",      # Password entry step
]


def is_auth_redirect(url: str) -> bool:
    """Return True if the URL indicates an Amazon login redirect."""
    return any(pattern in url for pattern in AUTH_REDIRECT_PATTERNS)


def launch_browser(headed: bool = False, profile_dir: Optional[Path] = None):
    """Launch Firefox with persistent profile. Returns (playwright, context).

    The caller is responsible for closing the context when done.

    Args:
        headed: If True, launch visible browser window (for auth and heal).
        profile_dir: Path to Firefox profile directory. Defaults to ~/.amazon-fresh-session/.

    Raises:
        ImportError: If playwright is not installed.
        RuntimeError: If profile directory doesn't exist and headed=False
                      (session has never been initialized).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "playwright not installed. Run: pip install playwright && playwright install firefox"
        )

    profile = profile_dir or DEFAULT_PROFILE_DIR

    if not headed and not profile.exists():
        raise RuntimeError(
            f"Amazon Fresh session profile not found at {profile}. "
            "Run 'amazon-fresh auth' on your Mac first, then transfer the profile to this machine."
        )

    profile.mkdir(parents=True, exist_ok=True)

    p = sync_playwright().start()
    context = p.firefox.launch_persistent_context(
        user_data_dir=str(profile),
        headless=not headed,
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) "
            "Gecko/20100101 Firefox/120.0"
        ),
    )
    return p, context


def get_page(context, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000):
    """Navigate to a URL and return the page. Detects auth redirects.

    Args:
        context: Playwright browser context.
        url: URL to navigate to.
        wait_until: Playwright wait_until event.
        timeout: Navigation timeout in ms.

    Raises:
        PermissionError: If Amazon redirects to login page.
    """
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(url, wait_until=wait_until, timeout=timeout)

    # Wait a moment for any redirect to settle
    page.wait_for_timeout(1500)

    if is_auth_redirect(page.url):
        raise PermissionError(
            f"Amazon session expired (redirected to {page.url}). "
            "Run 'amazon-fresh auth' on your Mac and re-transfer the browser profile."
        )

    return page


def close_browser(p, context) -> None:
    """Close context and stop playwright."""
    try:
        context.close()
    except Exception:
        pass
    try:
        p.stop()
    except Exception:
        pass
