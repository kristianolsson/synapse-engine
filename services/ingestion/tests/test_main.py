import logging
from pathlib import Path

import pytest
from services.ingestion.registry import ServiceRegistry, RegistryError
from services.ingestion.main import validate_startup, sync_vault


def test_validate_startup_raises_on_registry_error(tmp_path, monkeypatch):
    # No manifests at all -> registry has zero channels -> RegistryError
    registry = ServiceRegistry.discover(tmp_path)
    with pytest.raises(RegistryError):
        validate_startup(registry, {"email"})


def test_sync_vault_does_not_raise_when_apply_fails(tmp_path, monkeypatch, caplog):
    """A misconfigured VAULT_PATH (e.g. the .env.example placeholder, or a
    directory the process has no permission to write to) must not crash the
    process before listener threads start — that would crash-loop under a
    supervisor with no listener ever coming up."""
    registry = ServiceRegistry.discover(tmp_path)

    def boom(*args, **kwargs):
        raise PermissionError("Permission denied: '/path/to/notes'")

    monkeypatch.setattr("services.ingestion.main.apply", boom)

    with caplog.at_level(logging.ERROR, logger="services.ingestion.main"):
        sync_vault(registry, set(), Path("/path/to/notes"), tmp_path)  # must not raise

    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_sync_vault_commits_when_apply_reports_changes(tmp_path, monkeypatch):
    registry = ServiceRegistry.discover(tmp_path)
    calls = []
    monkeypatch.setattr("services.ingestion.main.apply", lambda *a, **k: ["CLAUDE.md"])
    monkeypatch.setattr("services.ingestion.main.commit_and_push_if_changed", lambda *a, **k: calls.append(a))

    sync_vault(registry, set(), tmp_path, tmp_path)

    assert calls == [(tmp_path, ["CLAUDE.md"])]
