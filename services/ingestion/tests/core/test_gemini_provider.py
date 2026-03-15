"""
Tests for GeminiProvider model fallback sequence.
"""

import pytest
import subprocess
import json
from unittest.mock import patch, MagicMock
from services.ingestion.providers.gemini import GeminiProvider
from services.ingestion import config

@pytest.fixture
def mock_config():
    with patch("services.ingestion.providers.gemini.config") as m:
        m.GEMINI_CMD = "gemini"
        m.VAULT_PATH = "/tmp/vault"
        m.GEMINI_FALLBACK_MODELS = ["pro", "flash"]
        m.GEMINI_MAX_RETRIES = 3
        m.GEMINI_TIMEOUT_SECONDS = 120
        yield m

@pytest.fixture
def provider():
    return GeminiProvider()

def test_model_sequence_no_specific_model(provider, mock_config):
    """Verify that None is tried first if no model is requested."""
    with patch("subprocess.run") as mock_run:
        # Simulate failure for all attempts
        mock_run.return_value = MagicMock(returncode=1, stderr="Error", stdout="")
        
        provider.generate_response("test prompt")
        
        assert mock_run.call_count == 3
        
        # 1st attempt: no model flag (model=None)
        args_1 = mock_run.call_args_list[0][0][0]
        assert not any(arg.startswith("--model=") for arg in args_1)
        
        # 2nd attempt: pro
        args_2 = mock_run.call_args_list[1][0][0]
        assert "--model=pro" in args_2
        
        # 3rd attempt: flash
        args_3 = mock_run.call_args_list[2][0][0]
        assert "--model=flash" in args_3

def test_model_sequence_with_specific_model(provider, mock_config):
    """Verify that requested model is tried first."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="Error", stdout="")
        
        provider.generate_response("test prompt", model="auto")
        
        assert mock_run.call_count == 3
        
        # 1st attempt: auto
        args_1 = mock_run.call_args_list[0][0][0]
        assert "--model=auto" in args_1
        
        # 2nd attempt: pro
        args_2 = mock_run.call_args_list[1][0][0]
        assert "--model=pro" in args_2
        
        # 3rd attempt: flash
        args_3 = mock_run.call_args_list[2][0][0]
        assert "--model=flash" in args_3
