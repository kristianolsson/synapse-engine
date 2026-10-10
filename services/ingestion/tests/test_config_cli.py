"""Tests for Claude CLI path resolution in config.py."""

from services.ingestion import config


def _make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


def test_explicit_claude_cmd_wins(tmp_path, monkeypatch):
    _make_exe(tmp_path / "node_modules" / ".bin" / "claude")
    monkeypatch.setenv("CLAUDE_CMD", "/custom/claude")
    assert config._resolve_claude_cmd(repo_root=tmp_path) == "/custom/claude"


def test_update_cli_install_preferred_over_path(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CMD", raising=False)
    local = tmp_path / "node_modules" / ".bin" / "claude"
    _make_exe(local)
    on_path = tmp_path / "bin" / "claude"
    _make_exe(on_path)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert config._resolve_claude_cmd(repo_root=tmp_path) == str(local)


def test_non_executable_local_install_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CMD", raising=False)
    local = tmp_path / "node_modules" / ".bin" / "claude"
    local.parent.mkdir(parents=True)
    local.write_text("not executable")
    on_path = tmp_path / "bin" / "claude"
    _make_exe(on_path)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert config._resolve_claude_cmd(repo_root=tmp_path) == str(on_path)


def test_falls_back_to_path_without_local_install(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CMD", raising=False)
    on_path = tmp_path / "bin" / "claude"
    _make_exe(on_path)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert config._resolve_claude_cmd(repo_root=tmp_path / "no-repo") == str(on_path)
