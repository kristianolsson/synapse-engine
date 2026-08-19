import json
import os
from unittest.mock import patch, MagicMock, mock_open
from services.ingestion.providers.agy import AgyProvider
from services.ingestion import config

class TestAgyProvider:
    def setup_method(self):
        self.provider = AgyProvider()
        # Mock config values
        self.original_cmd = config.AGY_CMD
        self.original_timeout = config.AGY_TIMEOUT_SECONDS
        self.original_retries = config.AGY_MAX_RETRIES

        config.AGY_CMD = "/usr/local/bin/agy"
        config.AGY_TIMEOUT_SECONDS = 30
        config.AGY_MAX_RETRIES = 3

    def teardown_method(self):
        config.AGY_CMD = self.original_cmd
        config.AGY_TIMEOUT_SECONDS = self.original_timeout
        config.AGY_MAX_RETRIES = self.original_retries

    @patch("subprocess.run")
    @patch("services.ingestion.providers.agy.os.path.exists")
    @patch("services.ingestion.providers.agy.open", new_callable=mock_open)
    def test_generate_response_success(self, mock_file_open, mock_exists, mock_run):
        # Setup CLI response
        mock_run.return_value = MagicMock(
            returncode=0, stdout="This is the answer from Antigravity.", stderr=""
        )
        
        # Setup Cache config response
        mock_exists.return_value = True
        mock_file_open.return_value.read.return_value = json.dumps({
            os.path.realpath(config.VAULT_PATH): "agy-session-123"
        })

        result = self.provider.generate_response("hello")

        assert result.is_error is False
        assert result.requires_reply is True
        assert result.text == "This is the answer from Antigravity."
        assert result.session_id == "agy-session-123"

        # Verify command flags order
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == config.AGY_CMD
        assert call_args[1] == "--dangerously-skip-permissions"
        assert call_args[2] == "--print"
        assert call_args[3] == "hello"

    @patch("subprocess.run")
    @patch("services.ingestion.providers.agy.os.path.exists")
    @patch("services.ingestion.providers.agy.open", new_callable=mock_open)
    def test_generate_response_silent_success(self, mock_file_open, mock_exists, mock_run):
        # Setup CLI response
        mock_run.return_value = MagicMock(
            returncode=0, stdout="SYNAPSE_OK", stderr=""
        )
        
        # Setup Cache config response
        mock_exists.return_value = True
        mock_file_open.return_value.read.return_value = json.dumps({
            os.path.realpath(config.VAULT_PATH): "agy-session-123"
        })

        result = self.provider.generate_response("hello")

        assert result.is_error is False
        assert result.requires_reply is False
        assert result.text == ""
        assert result.session_id == "agy-session-123"

    @patch("subprocess.run")
    @patch("services.ingestion.providers.agy.os.path.exists")
    @patch("services.ingestion.providers.agy.open", new_callable=mock_open)
    def test_generate_response_session_resume(self, mock_file_open, mock_exists, mock_run):
        # Setup CLI response
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Response with session", stderr=""
        )
        mock_exists.return_value = False

        result = self.provider.generate_response("hello", session_id="existing-session-abc")

        assert result.is_error is False
        assert result.session_id == ""  # cache file exists is False, so session_id is empty

        # Verify command flags order includes --conversation before --print
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == config.AGY_CMD
        assert call_args[1] == "--dangerously-skip-permissions"
        assert call_args[2] == "--conversation"
        assert call_args[3] == "existing-session-abc"
        assert call_args[4] == "--print"
        assert call_args[5] == "hello"

    @patch("subprocess.run")
    def test_generate_response_error_exit_code(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stdout="", stderr="Internal agent error"
        )

        result = self.provider.generate_response("hello", auto_retry=False)

        assert result.is_error is True
        assert result.requires_reply is True
        assert "Internal agent error" in result.text
        assert result.return_code == 2

    @patch("subprocess.run")
    def test_retry_logic(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="Connection failed"),
            MagicMock(returncode=0, stdout="Second attempt success", stderr="")
        ]

        result = self.provider.generate_response("hello", auto_retry=True)

        assert result.is_error is False
        assert result.requires_reply is True
        assert result.text == "Second attempt success"
        assert mock_run.call_count == 2

    def test_cleanup_session_noop(self):
        # Agy cleanup is a no-op, shouldn't raise any errors
        self.provider.cleanup_session("session-id")

    @patch("subprocess.run")
    def test_generate_response_merges_extra_env(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.provider.generate_response("test", extra_env={"SYNAPSE_SESSION_KEY": "user-1"})

        called_env = mock_run.call_args.kwargs["env"]
        assert called_env["SYNAPSE_SESSION_KEY"] == "user-1"
