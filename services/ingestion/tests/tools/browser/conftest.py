"""Shared fixtures for browser tests."""

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def rt_root(monkeypatch):
    """Short runtime + profile roots under /tmp (unix socket paths must stay < 104 bytes)."""
    root = Path(tempfile.mkdtemp(prefix="sbt-", dir="/tmp"))
    monkeypatch.setenv("SYNAPSE_BROWSER_RUNTIME_DIR", str(root / "rt"))
    monkeypatch.setenv("SYNAPSE_BROWSER_PROFILE_DIR", str(root / "profiles"))
    yield root
    shutil.rmtree(root, ignore_errors=True)
