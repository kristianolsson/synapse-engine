"""Drives the real login flow in a real (headless) Chrome against a local mock
of E*TRADE's pages — same ids/paths the 2026-11-22 QNAP spike observed."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.ingestion.tools.chrome.launcher import find_chrome
from services.ingestion.tools.stocks import etrade_login
from services.ingestion.tools.stocks.etrade_login import LoginRejected, StepUpRequired


def _chrome_available():
    try:
        find_chrome()
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chrome_available(), reason="Google Chrome / Playwright not installed")

LOGIN_PAGE = """<!doctype html><title>Log on</title>
<form id="f"><input id="USER"><input id="password" type="password">
<button id="mfaLogonButton" type="submit">Log on</button></form>
<div id="err"></div>
<script>
document.getElementById('f').onsubmit = (e) => {
  e.preventDefault();
  window.__typedUser = document.getElementById('USER').value;
  const outcome = "%(outcome)s";
  if (outcome === 'reject') {
    const d = document.getElementById('err'); d.setAttribute('role', 'alert');
    d.textContent = "We're unable to process your request, please try again.";
  } else if (outcome === 'stepup') { location.href = '/etx/pxy/login/step-up'; }
  else { location.href = '/etx/hw/v2/accountshome'; }
};
</script>"""

STEP_UP_PAGE = """<!doctype html><title>Log on</title>
<button id="send-otp-btn" onclick="this.remove();document.getElementById('f').style.display='block'">Send code</button>
<form id="f" style="display:none">
<label for="otp">Verification code</label><input id="otp" name="otp" type="text" autocomplete="one-time-code">
<input type="radio" name="saveDevice" id="Yes"><label for="Yes">Yes, save this device.</label>
<input type="radio" name="saveDevice" id="No"><label for="No">No, do not save this device.</label>
<button id="verify-otp-btn" type="submit">Submit</button></form>
<script>
document.getElementById('f').onsubmit = (e) => {
  e.preventDefault();
  if (!document.getElementById('Yes').checked || document.getElementById('otp').value !== '123456') return;
  location.href = '/etx/hw/v2/accountshome';
};
</script>"""

HOME_PAGE = "<!doctype html><title>E*TRADE - Complete View</title>home"

AUTHORIZE_PAGE = """<!doctype html><title>Authorize</title>
<button onclick="document.body.innerHTML='<input type=text value=\\'AB12CD\\'>'">Accept</button>"""


@pytest.fixture
def site():
    state = {"outcome": "home"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            pages = {
                "/etx/pxy/login": LOGIN_PAGE % state,
                "/etx/pxy/login/step-up": STEP_UP_PAGE,
                "/etx/hw/v2/accountshome": HOME_PAGE,
                "/authorize": AUTHORIZE_PAGE,
            }
            body = pages.get(path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((body or "not found").encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


@pytest.fixture
def page(tmp_path):
    from playwright.sync_api import sync_playwright
    from services.ingestion.tools.chrome import chrome_cdp

    with sync_playwright() as p:
        with chrome_cdp(p, tmp_path / "profile", headless=True) as context:
            yield context.new_page()


def _login(page, site, **kw):
    etrade_login.login(page, "user1", "pw", login_url=site["base"] + "/etx/pxy/login",
                       timeout_ms=kw.pop("timeout_ms", 10000), **kw)


def test_trusted_device_logs_in_with_typed_credentials(page, site):
    _login(page, site)
    assert page.url.endswith("/etx/hw/v2/accountshome")


def test_rejection_banner_raises_login_rejected(page, site):
    site["outcome"] = "reject"
    with pytest.raises(LoginRejected, match="unable to process"):
        _login(page, site)


def test_step_up_unattended_raises_without_prompting(page, site):
    site["outcome"] = "stepup"
    with pytest.raises(StepUpRequired):
        _login(page, site)


def test_step_up_attended_sends_code_and_saves_device(page, site):
    site["outcome"] = "stepup"
    prompts = []
    _login(page, site, code_prompt=lambda: prompts.append(1) or "123456")
    assert prompts == [1]
    assert page.url.endswith("/etx/hw/v2/accountshome")


def test_step_up_attended_empty_code_raises(page, site):
    site["outcome"] = "stepup"
    with pytest.raises(StepUpRequired, match="no SMS code"):
        _login(page, site, code_prompt=lambda: "  ")


def test_authorize_returns_verifier(page, site):
    assert etrade_login.authorize(page, site["base"] + "/authorize") == "AB12CD"


def test_human_type_uses_key_presses_not_fill():
    calls = []

    class FakeLocator:
        def click(self):
            calls.append("click")

        def press_sequentially(self, value, delay):
            calls.append(("type", value, delay))

        def fill(self, value):  # must never be used
            raise AssertionError("fill() used")

    class FixedRng:
        def randint(self, a, b):
            return a

        def uniform(self, a, b):
            return 0

    etrade_login.human_type(FakeLocator(), "secret", rng=FixedRng())
    assert calls == ["click", ("type", "secret", 80)]


def test_step_up_wrong_code_raises_step_up_required(page, site):
    site["outcome"] = "stepup"
    with pytest.raises(StepUpRequired, match="did not complete"):
        _login(page, site, code_prompt=lambda: "000000", timeout_ms=3000)


def test_wait_past_login_survives_navigation_race():
    from playwright.sync_api import Error as PlaywrightError

    class FakePage:
        def __init__(self):
            self.url = "https://x/etx/pxy/login"

        def evaluate(self, js):
            self.url = "https://x/etx/hw/v2/accountshome"
            raise PlaywrightError("Execution context was destroyed")

        def wait_for_timeout(self, ms):
            pass

    etrade_login._wait_past_login(FakePage(), 5000)
