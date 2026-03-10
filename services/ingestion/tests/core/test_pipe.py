"""Tests for the prompt standardization and AI provider pipe."""

from unittest.mock import patch, MagicMock

from services.ingestion.core.pipe import (
    IncomingMessage,
    PipeResult,
    build_prompt,
    pipe_to_gemini,
)
from services.ingestion.providers.base import ProviderResult

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


# ── pipe_to_gemini delegation tests ─────────────────────────────────

class TestPipeToGeminiDelegation:
    @patch("services.ingestion.core.pipe.get_provider")
    def test_delegation_success(self, mock_get_provider):
        # Mock provider
        mock_provider = MagicMock()
        mock_get_provider.return_value = mock_provider

        # Mock result
        mock_provider.generate_response.return_value = ProviderResult(
            text="Done",
            is_error=False,
            requires_reply=True,
            session_id="session-123"
        )

        result = pipe_to_gemini("test prompt", session_id="old-session")

        mock_provider.generate_response.assert_called_once_with("test prompt", session_id="old-session", attachments=[], model=None, auto_retry=True, cleanup_on_error=False)

        assert isinstance(result, PipeResult)
        assert result.output == "Done"
        assert result.is_error is False
        assert result.requires_reply is True
        assert result.session_id == "session-123"
