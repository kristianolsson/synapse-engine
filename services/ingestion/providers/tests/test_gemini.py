
import pytest
from unittest.mock import patch, MagicMock
import subprocess
from services.ingestion.providers.gemini import GeminiProvider
from services.ingestion import config

class TestGeminiProvider:
    def setup_method(self):
        self.provider = GeminiProvider()
        # Mock config values
        self.original_cmd = config.GEMINI_CMD
        self.original_timeout = config.GEMINI_TIMEOUT_SECONDS
        self.original_retries = config.GEMINI_MAX_RETRIES
        self.original_models = config.GEMINI_FALLBACK_MODELS
        
        config.GEMINI_CMD = "/usr/local/bin/gemini"
        config.GEMINI_TIMEOUT_SECONDS = 30
        config.GEMINI_MAX_RETRIES = 3
        config.GEMINI_FALLBACK_MODELS = ["pro", "flash"]

    def teardown_method(self):
        config.GEMINI_CMD = self.original_cmd
        config.GEMINI_TIMEOUT_SECONDS = self.original_timeout
        config.GEMINI_MAX_RETRIES = self.original_retries
        config.GEMINI_FALLBACK_MODELS = self.original_models

    @patch("subprocess.run")
    def test_generate_response_success(self, mock_run):
        json_output = '{"response": "SYNAPSE_OK", "session_id": "123"}'
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json_output, stderr=""
        )
        
        result = self.provider.generate_response("test")
        
        assert result.is_error is False
        assert result.text == ""
        assert result.session_id == "123"

    @patch("subprocess.run")
    def test_generate_response_error_clean(self, mock_run):
        raw_error = """GaxiosError: [{
          "error": {
            "code": 429,
            "message": "No capacity",
            "status": "RESOURCE_EXHAUSTED"
          }
        }]"""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr=raw_error
        )
        
        result = self.provider.generate_response("test")
        
        assert result.is_error is True
        assert "RESOURCE_EXHAUSTED" in result.text

    @patch("subprocess.run")
    def test_retry_logic(self, mock_run):
        # First attempt fails, second succeeds
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="Quota error"),
            MagicMock(returncode=0, stdout='{"response": "OK"}', stderr="")
        ]
        
        result = self.provider.generate_response("test")
        
        assert result.is_error is False
        assert "⚠️ Processed using fallback model" in result.text
        assert "OK" in result.text
        assert mock_run.call_count == 2
