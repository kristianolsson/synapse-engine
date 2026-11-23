"""Tests for ClaudeProvider permission flags (auto mode vs bypass rollback)."""

import json
from unittest.mock import patch, MagicMock

import pytest

from services.ingestion import config as real_config
from services.ingestion.providers.claude import ClaudeProvider, _summarize_denial


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


from services.ingestion.providers.claude_agent_settings import (
    build_agent_settings, agent_settings_json,
)


def test_settings_deny_rules_cover_secret_paths():
    deny = build_agent_settings("/home/u", "/app/engine")["permissions"]["deny"]
    expected_read = [
        "Read(//proc/**)",
        "Read(//home/u/.ssh/**)",
        "Read(//home/u/.claude/.credentials.json)",
        "Read(//home/u/.claude/settings*.json)",
        "Read(//home/u/.etrade_tokens)",
        "Read(//home/u/.etrade_chrome_profile/**)",
        "Read(//home/u/.synapse-browser/**)",
        "Read(//home/u/.amazon-fresh-session/**)",
        "Read(//app/engine/.env)",
        "Read(//app/engine/token.json)",
        "Read(//app/engine/credentials.json)",
        "Read(//app/engine/smartthings_token.json)",
    ]
    for rule in expected_read:
        assert rule in deny
        assert rule.replace("Read(", "Edit(", 1) in deny
    assert "Edit(//app/engine/**)" in deny
    for rule in ["Bash(env)", "Bash(env *)", "Bash(printenv*)", "Bash(cat /proc/*)"]:
        assert rule in deny


def test_settings_keep_session_transcripts_readable():
    deny = build_agent_settings("/home/u", "/app/engine")["permissions"]["deny"]
    assert not any(r.startswith("Read(//home/u/.claude/**") or r == "Read(//home/u/.claude)" for r in deny)
    assert not any("projects" in r for r in deny)


def test_settings_only_contains_deny():
    s = build_agent_settings("/home/u", "/app/engine")
    assert list(s.keys()) == ["permissions"]
    assert list(s["permissions"].keys()) == ["deny"]


def test_agent_settings_json_is_valid_json():
    data = json.loads(agent_settings_json())
    assert data["permissions"]["deny"]


def test_auto_mode_passes_settings(mock_config):
    _, cmds = _run_and_capture(mock_config)
    cmd = cmds[0]
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert "Read(//proc/**)" in settings["permissions"]["deny"]


def test_bypass_mode_has_no_settings(mock_config):
    mock_config.CLAUDE_PERMISSION_MODE = "bypass"
    _, cmds = _run_and_capture(mock_config)
    assert "--settings" not in cmds[0]


def test_summarize_denial_bash_truncates():
    d = {"tool_name": "Bash", "tool_use_id": "x", "tool_input": {"command": "curl " + "a" * 500}}
    s = _summarize_denial(d)
    assert s["tool"] == "Bash"
    assert s["summary"].startswith("curl ")
    assert len(s["summary"]) <= 200


def test_summarize_denial_file_tool_uses_path():
    d = {"tool_name": "Read", "tool_use_id": "x", "tool_input": {"file_path": "/x/secret"}}
    assert _summarize_denial(d) == {"tool": "Read", "summary": "/x/secret"}


def test_summarize_denial_tolerates_garbage():
    assert _summarize_denial({}) == {"tool": "unknown", "summary": ""}


def test_denials_land_in_stats_and_log(mock_config, caplog):
    denial = {"tool_name": "Bash", "tool_use_id": "t", "tool_input": {"command": "curl http://x"}}
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout=_ok_stdout(permission_denials=[denial]), stderr="")
        with caplog.at_level("WARNING"):
            res = ClaudeProvider().generate_response("p")
    assert res.stats["permission_denials"] == [{"tool": "Bash", "summary": "curl http://x"}]
    assert any("blocked" in r.message.lower() and "curl http://x" in r.getMessage() for r in caplog.records)


def test_no_denials_key_when_empty(mock_config):
    res, _ = _run_and_capture(mock_config)
    assert "permission_denials" not in (res.stats or {})
