"""Shared fixtures for browser tests."""

import shutil
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rt_root(monkeypatch):
    """Short runtime + profile roots under /tmp (unix socket paths must stay < 104 bytes)."""
    root = Path(tempfile.mkdtemp(prefix="sbt-", dir="/tmp"))
    monkeypatch.setenv("SYNAPSE_BROWSER_RUNTIME_DIR", str(root / "rt"))
    monkeypatch.setenv("SYNAPSE_BROWSER_PROFILE_DIR", str(root / "profiles"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


REQUEST_LOG: list = []  # (Host header, path) of every GET the fixture server receives


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, content_type: str, extra_headers=()):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        REQUEST_LOG.append((self.headers.get("Host", ""), url.path))
        if url.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", parse_qs(url.query)["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if url.path == "/slow":
            time.sleep(int(parse_qs(url.query).get("ms", ["1000"])[0]) / 1000)
            return self._send(b"Loaded results", "text/plain")
        if url.path == "/hang":
            time.sleep(60)
            return None
        if url.path == "/download":
            return self._send(b"a,b\n1,2\n", "application/octet-stream",
                              [("Content-Disposition", 'attachment; filename="report.csv"')])
        return super().do_GET()


@pytest.fixture(scope="session")
def site():
    """Base URL of a local HTTP server serving fixtures/ plus /slow, /hang, /download, /redirect?to=."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest_asyncio.fixture
async def session(tmp_path):
    from services.ingestion.tools.browser.session import Session

    s = Session(profile=tmp_path / "profile", artifacts=tmp_path / "artifacts", allow_private=True)
    await s.start()
    yield s
    await s.close()
