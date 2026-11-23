"""Tests for ClaudeProvider permission flags (auto mode vs bypass rollback)."""

import json
from unittest.mock import patch, MagicMock

import pytest

from services.ingestion import config as real_config
from services.ingestion.providers.claude import ClaudeProvider


def _ok_stdout(**extra):
    data = {"result": "hi", "session_id": "s1", "is_error": False}
    data.update(extra)
    return json.dumps(data)


@pytest.fixture
def mock_config():
    with patch("services.ingestion.providers.claude.config") as m:
        m.CLAUDE_CMD = "claude"
        m.VAULT_PATH = "/tmp/vault"
        m.CUSTOM_BIN_PATH = "/tmp/bin"
        m.CLAUDE_DEFAULT_MODEL = "sonnet"
        m.CLAUDE_WORK_MODEL = "opus"
        m.CLAUDE_FALLBACK_MODELS = []
        m.CLAUDE_MAX_RETRIES = 1
        m.CLAUDE_TIMEOUT_SECONDS = 60
        m.CLAUDE_MAX_BUDGET_USD = None
        m.CLAUDE_PERMISSION_MODE = "auto"
        yield m


def _run_and_capture(mock_config, **kwargs):
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout=_ok_stdout(), stderr="")
        res = ClaudeProvider().generate_response("p", **kwargs)
        return res, [c.args[0] for c in run.call_args_list]


def test_auto_mode_flags(mock_config):
    _, cmds = _run_and_capture(mock_config)
    cmd = cmds[0]
    assert "--dangerously-skip-permissions" not in cmd
    i = cmd.index("--permission-mode")
    assert cmd[i + 1] == "auto"
    j = cmd.index("--permission-prompts")
    assert cmd[j + 1] == "none"


def test_bypass_mode_flags(mock_config):
    mock_config.CLAUDE_PERMISSION_MODE = "bypass"
    _, cmds = _run_and_capture(mock_config)
    cmd = cmds[0]
    assert "--dangerously-skip-permissions" in cmd
    assert "--permission-mode" not in cmd
    assert "--permission-prompts" not in cmd


def test_resume_fallback_uses_same_permission_flags(mock_config):
    """The fresh-session retry after a failed --resume must not drop back to bypass."""
    with patch("subprocess.run") as run:
        run.side_effect = [
            MagicMock(returncode=1, stdout=json.dumps({"result": "No conversation found", "is_error": True}), stderr=""),
            MagicMock(returncode=0, stdout=_ok_stdout(), stderr=""),
        ]
        ClaudeProvider().generate_response("p", session_id="old")
        cmds = [c.args[0] for c in run.call_args_list]
    assert len(cmds) == 2
    assert "--resume" in cmds[0] and "--resume" not in cmds[1]
    for cmd in cmds:
        assert "--dangerously-skip-permissions" not in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "auto"


@pytest.mark.parametrize("raw,expected", [
    ("auto", "auto"), ("bypass", "bypass"), (" Bypass ", "bypass"),
    ("", "auto"), ("yolo", "auto"),
])
def test_parse_permission_mode(raw, expected):
    assert real_config._parse_permission_mode(raw) == expected
