"""E*TRADE web login + OAuth authorize, driven on a Playwright Page.

Behaviour comes from the 2026-11-22 QNAP spike (vault: synapse-browser-bridge):
- credentials must be typed key by key — instant fill() gets "We're unable to
  process your request" even from a trusted device;
- an untrusted device lands on /etx/pxy/login/step-up, where the SMS is only
  sent after clicking "Send code"; answering "Yes, save this device" makes the
  profile trusted so later logins skip SMS.
"""
import logging
import random
import re
import time
from typing import Callable, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

logger = logging.getLogger("options-bot")

LOGIN_URL = "https://us.etrade.com/etx/pxy/login"

ERRORS_JS = """() => [...document.querySelectorAll('[role=alert], .error, [class*=error], [class*=Error]')]
  .filter(e => e.offsetParent !== null && e.innerText.trim())
  .map(e => e.innerText.trim().slice(0, 200))"""

ACCEPT_SELECTORS = [
    '[value="Accept"]',
    'input[value="Accept"]',
    'button:has-text("Accept")',
    '#acceptSubmit',
    'input[type="submit"][value="Accept"]',
]


class LoginRejected(RuntimeError):
    """E*TRADE refused the login (error banner, or never left the login page)."""


class StepUpRequired(RuntimeError):
    """E*TRADE wants an SMS code (device not trusted) and no human is available."""


def human_type(locator, value: str, rng=random) -> None:
    locator.click()
    locator.press_sequentially(value, delay=rng.randint(80, 160))
    time.sleep(rng.uniform(0.4, 1.0))


def _is_bare_login(url: str) -> bool:
    return url.split("?")[0].rstrip("/").endswith("/etx/pxy/login")


def _is_step_up(url: str) -> bool:
    return "/etx/pxy/login/step-up" in url


def _wait_past_login(page, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if not _is_bare_login(page.url):
            return
        try:
            errors = page.evaluate(ERRORS_JS)
        except PlaywrightError:  # navigation committed mid-evaluate; re-check the URL
            page.wait_for_timeout(500)
            continue
        if errors:
            raise LoginRejected(f"E*TRADE rejected the login: {errors[0]}")
        page.wait_for_timeout(500)
    raise LoginRejected(f"E*TRADE login still on the login page after {timeout_ms // 1000}s")


def _complete_step_up(page, code_prompt: Callable[[], str], timeout_ms: int) -> None:
    logger.info("E*TRADE step-up: requesting SMS code")
    try:
        page.locator("#send-otp-btn").click()
        page.locator("#otp").wait_for(state="visible", timeout=30000)
    except PlaywrightTimeout as e:
        raise StepUpRequired(f"E*TRADE step-up did not complete: SMS code form not available ({e})") from e
    code = code_prompt().strip()
    if not code:
        raise StepUpRequired("E*TRADE step-up: no SMS code entered")
    try:
        human_type(page.locator("#otp"), code)
        save_yes = page.locator('input[name="saveDevice"][id="Yes"]')
        try:
            save_yes.check(timeout=3000)
        except Exception:
            page.locator('label[for="Yes"]').click(timeout=3000)  # styled radios hide the input
        page.locator("#verify-otp-btn").click()
        page.wait_for_url(lambda u: "/etx/pxy/login" not in u, timeout=timeout_ms)
    except PlaywrightTimeout as e:
        raise StepUpRequired(f"E*TRADE step-up did not complete: code rejected or page stuck ({e})") from e


def login(page, username: str, password: str, *, totp_secret: Optional[str] = None,
          code_prompt: Optional[Callable[[], str]] = None, timeout_ms: int = 90000,
          login_url: str = LOGIN_URL) -> None:
    logger.info("Navigating to E*TRADE login page...")
    page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)

    human_type(page.locator("#USER"), username)
    human_type(page.locator("#password"), password)
    if totp_secret:
        from pyotp import TOTP
        page.locator('[for="useSecurityCode"]').click()
        human_type(page.locator("#securityCode"), TOTP(totp_secret).now())
    page.locator("#mfaLogonButton").click()

    _wait_past_login(page, timeout_ms)
    if _is_step_up(page.url):
        if code_prompt is None:
            raise StepUpRequired("E*TRADE asked for an SMS code (device not trusted)")
        _complete_step_up(page, code_prompt, timeout_ms)

    try:  # intermediate "Continue" interstitials
        btn = page.locator('button:has-text("Continue")')
        btn.first.wait_for(state="visible", timeout=3000)
        btn.first.click()
        page.wait_for_timeout(2000)
    except PlaywrightTimeout:
        pass
    logger.info("E*TRADE login complete: %s", page.url.split("?")[0])


def authorize(page, authorize_url: str) -> str:
    logger.info("Navigating to OAuth authorization...")
    page.goto(authorize_url, wait_until="domcontentloaded", timeout=30000)

    try:
        page.locator(", ".join(ACCEPT_SELECTORS)).first.click(timeout=15000)
        logger.info("Clicked Accept button")
    except PlaywrightTimeout:
        logger.warning("Could not find Accept button, checking for verification code...")

    code = None
    try:
        code_input = page.locator('input[type="text"]').first
        code_input.wait_for(state="visible", timeout=10000)
        code = (code_input.get_attribute("value") or "").strip() or None
    except PlaywrightTimeout:
        pass
    if not code:
        matches = re.findall(r'code["\s:]+([A-Z0-9]{5,10})', page.content(), re.IGNORECASE)
        if matches:
            code = matches[0]
    if not code:
        try:
            page.screenshot(path="/tmp/etrade_auth_debug.png")
            logger.info("Screenshot saved to /tmp/etrade_auth_debug.png")
        except Exception:
            pass
        raise ValueError("Could not extract E*TRADE verification code from the authorize page")
    logger.info("Got verification code: %s...", code[:3])
    return code
