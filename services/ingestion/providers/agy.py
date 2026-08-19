import logging
import os
import subprocess
import json
from typing import Optional, List, Dict

from .. import config
from .base import AIProvider, ProviderResult, GLOBAL_PROVIDER_LOCK

logger = logging.getLogger(__name__)


class AgyProvider(AIProvider):
    """
    Implementation of Agy provider using the Antigravity CLI (agy).
    """

    def generate_response(self, prompt: str, session_id: Optional[str] = None, attachments: List[str] = [], model: Optional[str] = None, auto_retry: bool = True, cleanup_on_error: bool = False, extra_env: Optional[Dict[str, str]] = None) -> ProviderResult:
        """
        Execute the agy CLI with the given prompt inside the vault directory.
        """
        vault_path = config.VAULT_PATH

        # Build env with the Agy CLI's directory and our custom bin in PATH
        env = os.environ.copy()
        custom_bin = config.CUSTOM_BIN_PATH
        agy_dir = os.path.dirname(config.AGY_CMD)
        if agy_dir:
            env["PATH"] = custom_bin + ":" + agy_dir + ":" + env.get("PATH", "")
        else:
            env["PATH"] = custom_bin + ":" + env.get("PATH", "")
        if extra_env:
            env.update(extra_env)

        def _run_cmd(current_cmd: List[str]) -> ProviderResult:
            logger.debug("Waiting for GLOBAL_PROVIDER_LOCK...")
            with GLOBAL_PROVIDER_LOCK:
                logger.debug("Acquired GLOBAL_PROVIDER_LOCK for Agy CLI.")
                try:
                    result = subprocess.run(
                        current_cmd,
                        cwd=vault_path,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=config.AGY_TIMEOUT_SECONDS,
                        env=env,
                    )

                    stdout = result.stdout.strip()
                    stderr = result.stderr.strip()

                    if result.returncode != 0:
                        error_msg = stderr or stdout or f"Agy CLI exited with code {result.returncode}"
                        logger.error("Agy CLI error (code %d): %s", result.returncode, error_msg)
                        return ProviderResult(
                            is_error=True,
                            requires_reply=True,
                            text=error_msg,
                            return_code=result.returncode,
                            provider_name="agy"
                        )

                    # Extract the conversation ID from cache/last_conversations.json
                    returned_session_id = ""
                    cache_path = os.path.expanduser("~/.gemini/antigravity-cli/cache/last_conversations.json")
                    if os.path.exists(cache_path):
                        try:
                            with open(cache_path, "r") as f:
                                data = json.load(f)
                            abs_vault_path = os.path.realpath(vault_path)
                            returned_session_id = data.get(abs_vault_path, "")
                        except Exception as e:
                            logger.warning("Failed to read last_conversations.json: %s", e)

                    if stdout:
                        # Check for success signal code word
                        if stdout.strip() == "SYNAPSE_OK":
                            logger.info("Agy CLI completed successfully (SYNAPSE_OK)")
                            return ProviderResult(
                                is_error=False,
                                requires_reply=False,
                                text="",
                                return_code=0,
                                session_id=returned_session_id,
                                provider_name="agy"
                            )

                        # Non-empty response = agent wants to relay something (question/answer)
                        logger.info("Agy CLI returned response: %s", stdout[:200])
                        return ProviderResult(
                            is_error=False,
                            requires_reply=True,
                            text=stdout,
                            return_code=0,
                            session_id=returned_session_id,
                            provider_name="agy"
                        )

                    # Empty response = silent success
                    logger.info("Agy CLI completed successfully (silent response)")
                    return ProviderResult(
                        is_error=False,
                        requires_reply=False,
                        text="",
                        return_code=0,
                        session_id=returned_session_id,
                        provider_name="agy"
                    )

                except subprocess.TimeoutExpired:
                    logger.error("Agy CLI timed out after %ds", config.AGY_TIMEOUT_SECONDS)
                    return ProviderResult(
                        is_error=True,
                        requires_reply=True,
                        text=f"Agy CLI timed out after {config.AGY_TIMEOUT_SECONDS} seconds.",
                        return_code=-1,
                        provider_name="agy",
                    )
                except FileNotFoundError:
                    logger.error("Agy CLI not found at '%s'", config.AGY_CMD)
                    return ProviderResult(
                        is_error=True,
                        requires_reply=True,
                        text=f"Agy CLI not found at '{config.AGY_CMD}'. Is it installed and in PATH?",
                        return_code=-1,
                        provider_name="agy",
                    )
                except Exception as e:
                    logger.error("Unexpected error invoking Agy CLI: %s", e)
                    return ProviderResult(
                        is_error=True,
                        requires_reply=True,
                        text=str(e),
                        return_code=-1,
                        provider_name="agy"
                    )

        # Build cmd
        cmd = [config.AGY_CMD, "--dangerously-skip-permissions"]
        if session_id:
            cmd.extend(["--conversation", session_id])
        cmd.extend(["--print", prompt])

        logger.info("Piping prompt to Agy CLI (vault=%s, session_id=%s)", vault_path, session_id or "none")
        logger.debug("Prompt:\n%s", prompt)

        max_attempts = config.AGY_MAX_RETRIES if auto_retry else 1
        res = None
        for attempt in range(max_attempts):
            res = _run_cmd(cmd)
            if not res.is_error:
                return res
            logger.warning("Agy CLI attempt %d failed: %s", attempt + 1, res.text[:200])

        return res

    def cleanup_session(self, session_id: str) -> None:
        """
        Agy CLI does not support session deletion. No-op.
        """
        if not session_id:
            return
        logger.debug("cleanup_session called for Agy (no-op): %s", session_id)
