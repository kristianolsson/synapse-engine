from contextlib import contextmanager

import pytest

from services.ingestion.tools.stocks import wetrade_auth
from services.ingestion.tools.stocks.etrade_login import LoginRejected, StepUpRequired


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(wetrade_auth, "BROWSER_AUTH_AVAILABLE", True)
    return wetrade_auth.BrowserAuth("ck", "cs", "user", "pw", token_file=tmp_path / ".etrade_tokens")


@pytest.fixture
def fake_browser(monkeypatch):
    calls = {"login": [], "profile": None}

    @contextmanager
    def fake_sync_playwright():
        yield object()

    @contextmanager
    def fake_chrome_cdp(playwright, profile_dir, **kw):
        calls["profile"] = profile_dir

        class Ctx:
            def new_page(self):
                return "PAGE"
        yield Ctx()

    def fake_login(page, username, password, **kw):
        calls["login"].append(kw)
        if calls.get("raise"):
            raise calls["raise"]

    monkeypatch.setattr(wetrade_auth, "sync_playwright", fake_sync_playwright)
    monkeypatch.setattr(wetrade_auth, "chrome_cdp", fake_chrome_cdp)
    monkeypatch.setattr(wetrade_auth.etrade_login, "login", fake_login)
    monkeypatch.setattr(wetrade_auth.etrade_login, "authorize", lambda page, url: "VERIFIER")
    return calls


def test_uses_chrome_profile_next_to_token_file(auth, fake_browser, tmp_path):
    assert auth._do_browser_login("https://auth", headless=True) == "VERIFIER"
    assert fake_browser["profile"] == tmp_path / ".etrade_chrome_profile"


def test_unattended_never_prompts(auth, fake_browser):
    auth._do_browser_login("https://auth", headless=True, login_timeout_ms=60000)
    kw = fake_browser["login"][0]
    assert kw["code_prompt"] is None
    assert kw["timeout_ms"] == 60000


def test_attended_gets_terminal_prompt(auth, fake_browser):
    auth._do_browser_login("https://auth", headless=False)
    assert callable(fake_browser["login"][0]["code_prompt"])


@pytest.mark.parametrize("exc", [StepUpRequired("sms"), LoginRejected("nope")])
def test_deterministic_failures_are_not_retried(auth, fake_browser, exc):
    fake_browser["raise"] = exc
    with pytest.raises(type(exc)):
        auth._browser_login("https://auth", headless=True, max_retries=3)
    assert len(fake_browser["login"]) == 1


def test_other_failures_are_retried(auth, fake_browser):
    fake_browser["raise"] = RuntimeError("network blip")
    with pytest.raises(RuntimeError):
        auth._browser_login("https://auth", headless=True, max_retries=2)
    assert len(fake_browser["login"]) == 2


def test_load_tokens_oserror_degrades(auth, monkeypatch):
    auth.token_file.write_text("{}")
    def boom(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr("builtins.open", boom)
    assert auth._load_tokens() is False
