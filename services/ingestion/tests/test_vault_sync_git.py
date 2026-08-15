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
