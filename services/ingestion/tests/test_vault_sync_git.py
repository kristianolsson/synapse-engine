import logging
import subprocess
from pathlib import Path
import pytest
from services.ingestion.vault_sync import commit_and_push_if_changed


@pytest.fixture
def git_vault(tmp_path):
    vault = tmp_path / "notes"
    vault.mkdir()
    subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=vault, check=True)
    (vault / "file.md").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=vault, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=vault, check=True, capture_output=True)
    return vault


def test_noop_when_nothing_changed(git_vault):
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_vault, capture_output=True, text=True).stdout
    commit_and_push_if_changed(git_vault, changed=False)
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_vault, capture_output=True, text=True).stdout
    assert before == after


def test_commits_when_changed(git_vault):
    (git_vault / "file.md").write_text("modified by apply()")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_vault, capture_output=True, text=True).stdout
    commit_and_push_if_changed(git_vault, changed=True, push=False)
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_vault, capture_output=True, text=True).stdout
    assert before != after
    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=git_vault, capture_output=True, text=True).stdout
    assert "synapse-engine apply()" in log


def test_does_not_raise_when_vault_is_not_a_git_repo(tmp_path, caplog):
    """A git failure at boot must never propagate — it would crash the process
    before any listener thread starts, i.e. a supervised crash loop."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    (not_a_repo / "file.md").write_text("content")

    with caplog.at_level(logging.ERROR, logger="services.ingestion.vault_sync"):
        commit_and_push_if_changed(not_a_repo, changed=True, push=False)  # must not raise

    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "expected the git failure to be logged as an error"


def test_does_not_raise_when_subprocess_raises_called_process_error(git_vault, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git", "push"], stderr=b"remote rejected")

    monkeypatch.setattr("services.ingestion.vault_sync.subprocess.run", boom)

    with caplog.at_level(logging.ERROR, logger="services.ingestion.vault_sync"):
        commit_and_push_if_changed(git_vault, changed=True, push=True)  # must not raise

    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_push_failure_does_not_raise(git_vault, caplog):
    """No remote is configured, so `git push` genuinely fails here."""
    (git_vault / "file.md").write_text("modified by apply()")

    with caplog.at_level(logging.ERROR, logger="services.ingestion.vault_sync"):
        commit_and_push_if_changed(git_vault, changed=True, push=True)  # must not raise

    # The local commit still landed even though the push failed.
    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=git_vault,
                         capture_output=True, text=True).stdout
    assert "synapse-engine apply()" in log
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
