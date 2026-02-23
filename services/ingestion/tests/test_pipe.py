"""Tests for the prompt standardization and Gemini CLI pipe."""

import subprocess
from unittest.mock import patch, MagicMock, call

from services.ingestion.pipe import (
    IncomingMessage,
    PipeResult,
    build_prompt,
    pipe_to_gemini,
    _clean_error_message,
    config, # Import config to patch it
)


# ── build_prompt tests ──────────────────────────────────────────────


class TestBuildPrompt:
    def test_basic_email(self):
        msg = IncomingMessage(
            source_type="email",
            sender="user@example.com",
            subject="Buy groceries",
            body="Milk, eggs, bread",
        )
        prompt = build_prompt(msg)
        assert "Type: email" in prompt
        assert "Sender: user@example.com" in prompt
        assert "Subject: Buy groceries" in prompt
        assert "Context: Ingested via EMAIL" in prompt
        assert "Images: none" in prompt
        assert "Milk, eggs, bread" in prompt

    def test_with_images(self):
        msg = IncomingMessage(
            source_type="email",
            sender="user@example.com",
            subject="Photo note",
            body="Check this out",
            image_paths=["/tmp/img1.jpg", "/tmp/img2.png"],
        )
        prompt = build_prompt(msg)
        assert "Images: 2 attached" in prompt
        assert "/tmp/img1.jpg" in prompt
        assert "/tmp/img2.png" in prompt
        assert "**Attached Images (use read_file to analyze):**" in prompt

    def test_empty_body(self):
        msg = IncomingMessage(
            source_type="email",
            sender="user@example.com",
            subject="Empty",
        )
        prompt = build_prompt(msg)
        assert "---" in prompt
        assert "Subject: Empty" in prompt

    def test_telegram_type(self):
        msg = IncomingMessage(
            source_type="telegram",
            sender="12345",
            body="Hello from watch",
        )
        prompt = build_prompt(msg)
        assert "Type: telegram" in prompt
        assert "Context: Ingested via TELEGRAM" in prompt


# ── _clean_error_message tests ─────────────────────────────────────

class TestCleanErrorMessage:
    def test_extract_gaxios_json_error(self):
        raw = """Attempt 1 failed with status 429. Retrying... GaxiosError: [{
  "error": {
    "code": 429,
    "message": "No capacity available for model on server",
    "status": "RESOURCE_EXHAUSTED"
  }
}
]
    at Gaxios._request (node:internal)"""
        clean = _clean_error_message(raw)
        assert clean == "RESOURCE_EXHAUSTED: No capacity available for model on server"

    def test_extract_top_level_quota_error(self):
        raw = """Error when talking to Gemini API 
RetryableQuotaError: No capacity available
    at classifyGoogleError (file:///)"""
        clean = _clean_error_message(raw)
        assert clean == "Quota Error: No capacity available"

    def test_strip_node_warnings(self):
        raw = """(node:88843) [DEP0040] DeprecationWarning: The `punycode` module...
(Use `node --trace-deprecation ...`)
YOLO mode is enabled. All tool calls will be automatically approved.
Loaded cached credentials.
fatal: not a git repository"""
        clean = _clean_error_message(raw)
        assert clean == "fatal: not a git repository"

# ── pipe_to_gemini tests ────────────────────────────────────────────


class TestPipeToGemini:
    def setUp(self):
        config.GEMINI_CMD = "/usr/local/bin/gemini"
        config.VAULT_PATH = "/tmp/vault"
        config.GEMINI_TIMEOUT_SECONDS = 30
        config.GEMINI_MAX_RETRIES = 3
        config.GEMINI_FALLBACK_MODELS = ["pro", "flash"]

    @patch("services.ingestion.pipe.subprocess.run")
    def test_success_json_with_warnings(self, mock_run):
        """Ignore preamble and parse JSON response."""
        json_output = """
(node:1234) DeprecationWarning...
{
  "response": "Done!"
}
"""
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )
        result = pipe_to_gemini("test prompt")
        assert result.is_error is False
        assert result.requires_reply is True
        assert result.output == "Done!"
        assert result.return_code == 0

    @patch("services.ingestion.pipe.subprocess.run")
    def test_success_silent_with_json(self, mock_run):
        """JSON with empty response field = silent success."""
        json_output = '{"response": ""}'
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )
        result = pipe_to_gemini("test prompt")
        assert result.is_error is False
        assert result.requires_reply is False
        assert result.output == ""
        assert result.return_code == 0

    @patch("services.ingestion.pipe.subprocess.run")
    def test_success_silent(self, mock_run):
        """Empty stdout → success."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        result = pipe_to_gemini("test prompt")
        assert result.is_error is False
        assert result.requires_reply is False
        assert result.output == ""
        assert result.return_code == 0
        # Verify correct command structure
        # from services.ingestion import config # patched at module level
        call_args = mock_run.call_args
        # We don't verify exact args here as they might change with retry loop
        assert mock_run.call_count >= 1

    @patch("services.ingestion.pipe.subprocess.run")
    def test_clarification_output(self, mock_run):
        """Non-empty stdout → relay to user."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Which project should this go to?",
            stderr="",
        )
        result = pipe_to_gemini("test prompt")
        # Non-JSON output is treated as error/clarification requiring reply
        assert result.is_error is True 
        assert result.requires_reply is True
        assert "Which project" in result.output

    @patch("services.ingestion.pipe.subprocess.run")
    def test_cli_error(self, mock_run):
        """Non-zero return code → error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="fatal: repo locked",
        )
        result = pipe_to_gemini("test prompt")
        assert result.is_error is True
        assert result.requires_reply is True
        assert "repo locked" in result.output
        assert result.return_code == 1

    @patch("services.ingestion.pipe.subprocess.run")
    def test_resume_session_success(self, mock_run):
        """Passing a session_id should include --resume in the command."""
        json_output = '{"response": "SYNAPSE_OK", "session_id": "new-123"}'
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )
        result = pipe_to_gemini("test prompt", session_id="old-456")
        assert result.is_error is False
        assert result.requires_reply is False
        assert result.output == ""
        assert result.session_id == "new-123"
        
        args, _ = mock_run.call_args
        cmd = args[0]
        assert "--resume" in cmd
        assert "old-456" in cmd

    @patch("services.ingestion.pipe.subprocess.run")
    def test_resume_session_fallback(self, mock_run):
        """If --resume fails, it should strip the flag and try again."""
        json_output_fail = '{"error": "Session expired"}'
        json_output_success = '{"response": "SYNAPSE_OK", "session_id": "new-123"}'
        
        # 1. Fail with resume
        # 2. Succeed without resume
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout=json_output_fail, stderr=""),
            MagicMock(returncode=0, stdout=json_output_success, stderr="")
        ]
        
        result = pipe_to_gemini("test prompt", session_id="old-456")
        assert result.is_error is False
        assert result.requires_reply is False
        assert result.session_id == "new-123"
        assert mock_run.call_count == 2
        
        first_call = mock_run.call_args_list[0][0][0]
        second_call = mock_run.call_args_list[1][0][0]
        assert "--resume" in first_call
        assert "--resume" not in second_call

    @patch("services.ingestion.pipe.subprocess.run")
    def test_timeout(self, mock_run):
        """Timeout → error with message."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gemini", timeout=120)
        result = pipe_to_gemini("test prompt")
        assert result.is_error is True
        assert result.requires_reply is True
        assert "timed out" in result.output
        assert result.return_code == -1

    @patch("services.ingestion.pipe.subprocess.run")
    def test_cli_not_found(self, mock_run):
        """Missing CLI binary → helpful error."""
        mock_run.side_effect = FileNotFoundError()
        result = pipe_to_gemini("test prompt")
        assert result.is_error is True
        assert result.requires_reply is True
        assert "not found" in result.output
        assert result.return_code == -1

    @patch("services.ingestion.pipe.subprocess.run")
    def test_model_fallback(self, mock_run):
        """Fail on first model, succeed on second."""
        config.GEMINI_FALLBACK_MODELS = ["model1", "model2"]
        
        # 1. Fail with model1
        # 2. Succeed with model2
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="Quota exceeded", stderr=""),
            MagicMock(returncode=0, stdout='{"response": "OK"}', stderr="")
        ]
        
        result = pipe_to_gemini("test prompt")
        assert result.is_error is False
        assert result.output == "OK"
        assert mock_run.call_count == 2
        
        first_call = mock_run.call_args_list[0][0][0]
        second_call = mock_run.call_args_list[1][0][0]
        
        # Check command args for models
        assert any("--model=model1" in arg for arg in first_call)
        assert any("--model=model2" in arg for arg in second_call)

