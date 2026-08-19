
import json
from unittest.mock import patch, MagicMock
from services.ingestion.providers.claude import ClaudeProvider
from services.ingestion import config

class TestClaudeProvider:
    def setup_method(self):
        self.provider = ClaudeProvider()
        self.original_cmd = config.CLAUDE_CMD
        self.original_timeout = config.CLAUDE_TIMEOUT_SECONDS
        self.original_retries = config.CLAUDE_MAX_RETRIES
        self.original_models = config.CLAUDE_FALLBACK_MODELS
        self.original_budget = config.CLAUDE_MAX_BUDGET_USD

        config.CLAUDE_CMD = "/usr/local/bin/claude"
        config.CLAUDE_TIMEOUT_SECONDS = 30
        config.CLAUDE_MAX_RETRIES = 3
        config.CLAUDE_FALLBACK_MODELS = ["sonnet", "haiku"]
        config.CLAUDE_MAX_BUDGET_USD = None

    def teardown_method(self):
        config.CLAUDE_CMD = self.original_cmd
        config.CLAUDE_TIMEOUT_SECONDS = self.original_timeout
        config.CLAUDE_MAX_RETRIES = self.original_retries
        config.CLAUDE_FALLBACK_MODELS = self.original_models
        config.CLAUDE_MAX_BUDGET_USD = self.original_budget

    @patch("subprocess.run")
    def test_generate_response_success(self, mock_run):
        json_output = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "SYNAPSE_OK",
            "session_id": "abc-123",
            "total_cost_usd": 0.03,
            "duration_ms": 2500,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 100, "outputTokens": 50, "costUSD": 0.03}}
        })
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )

        result = self.provider.generate_response("test")

        assert result.is_error is False
        assert result.requires_reply is False
        assert result.text == ""
        assert result.session_id == "abc-123"

    @patch("subprocess.run")
    def test_generate_response_with_reply(self, mock_run):
        json_output = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Here is your answer.",
            "session_id": "abc-456",
            "total_cost_usd": 0.05,
            "duration_ms": 3000,
            "modelUsage": {}
        })
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )

        result = self.provider.generate_response("what time is it?")

        assert result.is_error is False
        assert result.requires_reply is True
        assert result.text == "Here is your answer."
        assert result.session_id == "abc-456"
        assert result.stats.get("total_cost_usd") == 0.05

    @patch("subprocess.run")
    def test_generate_response_error_exit_code(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Claude CLI crashed"
        )

        result = self.provider.generate_response("test")

        assert result.is_error is True
        assert "Claude CLI crashed" in result.text

    @patch("subprocess.run")
    def test_generate_response_error_flag(self, mock_run):
        json_output = json.dumps({
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "result": "Budget exceeded",
            "session_id": "",
        })
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )

        result = self.provider.generate_response("test")

        assert result.is_error is True
        assert "Budget exceeded" in result.text

    @patch("subprocess.run")
    def test_retry_logic(self, mock_run):
        # First attempt fails, second succeeds
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="Overloaded error"),
            MagicMock(returncode=0, stdout=json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "OK response",
                "session_id": "retry-123",
                "modelUsage": {}
            }), stderr="")
        ]

        result = self.provider.generate_response("test")

        assert result.is_error is False
        assert "⚠️ Processed using fallback model" in result.text
        assert "OK response" in result.text
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_cleanup_session_noop(self, mock_run):
        self.provider.cleanup_session("some-session-id")
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_session_resume(self, mock_run):
        json_output = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "SYNAPSE_OK",
            "session_id": "existing-session",
            "modelUsage": {}
        })
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )

        result = self.provider.generate_response("test", session_id="existing-session")

        assert result.is_error is False
        # Verify --resume was passed in the command
        call_args = mock_run.call_args[0][0]
        assert "--resume" in call_args
        assert "existing-session" in call_args

    @patch("subprocess.run")
    def test_generate_response_merges_extra_env(self, mock_run):
        json_output = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "SYNAPSE_OK", "session_id": "abc-123",
        })
        mock_run.return_value = MagicMock(returncode=0, stdout=json_output, stderr="")

        self.provider.generate_response("test", extra_env={"SYNAPSE_SESSION_KEY": "user-1"})

        called_env = mock_run.call_args.kwargs["env"]
        assert called_env["SYNAPSE_SESSION_KEY"] == "user-1"

    @patch("subprocess.run")
    def test_generate_response_extra_env_none_does_not_raise(self, mock_run):
        json_output = json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "SYNAPSE_OK", "session_id": "abc-123",
        })
        mock_run.return_value = MagicMock(returncode=0, stdout=json_output, stderr="")

        self.provider.generate_response("test")  # extra_env omitted entirely

        called_env = mock_run.call_args.kwargs["env"]
        assert "SYNAPSE_SESSION_KEY" not in called_env
