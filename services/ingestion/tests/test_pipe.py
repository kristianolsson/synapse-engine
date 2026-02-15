"""Tests for the prompt standardization and Gemini CLI pipe."""

import subprocess
from unittest.mock import patch, MagicMock

from services.ingestion.pipe import (
    IncomingMessage,
    PipeResult,
    build_prompt,
    pipe_to_gemini,
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
        assert "**Attached Images:**" in prompt

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


# ── pipe_to_gemini tests ────────────────────────────────────────────


class TestPipeToGemini:
    @patch("services.ingestion.pipe.subprocess.run")
    def test_success_silent(self, mock_run):
        """Empty stdout → success."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        result = pipe_to_gemini("test prompt")
        assert result.success is True
        assert result.output == ""
        assert result.return_code == 0
        # Verify correct command structure (-p flag)
        from services.ingestion import config
        mock_run.assert_called_with(
            [config.GEMINI_CMD, f"--prompt=test prompt", "--yolo"],
            cwd=config.VAULT_PATH,
            capture_output=True,
            text=True,
            timeout=config.GEMINI_TIMEOUT_SECONDS,
        )

    @patch("services.ingestion.pipe.subprocess.run")
    def test_clarification_output(self, mock_run):
        """Non-empty stdout → relay to user."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Which project should this go to?",
            stderr="",
        )
        result = pipe_to_gemini("test prompt")
        assert result.success is False
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
        assert result.success is False
        assert "repo locked" in result.output
        assert result.return_code == 1

    @patch("services.ingestion.pipe.subprocess.run")
    def test_timeout(self, mock_run):
        """Timeout → error with message."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gemini", timeout=120)
        result = pipe_to_gemini("test prompt")
        assert result.success is False
        assert "timed out" in result.output
        assert result.return_code == -1

    @patch("services.ingestion.pipe.subprocess.run")
    def test_cli_not_found(self, mock_run):
        """Missing CLI binary → helpful error."""
        mock_run.side_effect = FileNotFoundError()
        result = pipe_to_gemini("test prompt")
        assert result.success is False
        assert "not found" in result.output
        assert result.return_code == -1
