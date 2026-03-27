import logging
import os
import subprocess
import json
import threading
from typing import Optional, List

from .. import config
from .base import AIProvider, ProviderResult

logger = logging.getLogger(__name__)

# Global lock to prevent concurrent Claude CLI executions across threads
_claude_lock = threading.Lock()


class ClaudeProvider(AIProvider):
    """
    Implementation of Claude provider using the Claude Code CLI.
    """

    def generate_response(self, prompt: str, session_id: Optional[str] = None, attachments: List[str] = [], model: Optional[str] = None, auto_retry: bool = True, cleanup_on_error: bool = False) -> ProviderResult:
        """
        Execute the Claude CLI with the given prompt inside the vault directory.
        """
        vault_path = config.VAULT_PATH

        # Build env with the Claude CLI's directory in PATH
        env = os.environ.copy()
        claude_dir = os.path.dirname(config.CLAUDE_CMD)
        if claude_dir:
            env["PATH"] = claude_dir + ":" + env.get("PATH", "")

        def _run_cmd(current_cmd: list[str], stdin_prompt: str = "") -> ProviderResult:
            logger.debug("Waiting for Claude CLI lock...")
            with _claude_lock:
                logger.debug("Acquired Claude CLI lock.")
                try:
                    result = subprocess.run(
                        current_cmd,
                        cwd=vault_path,
                        capture_output=True,
                        text=True,
                        timeout=config.CLAUDE_TIMEOUT_SECONDS,
                        env=env,
                        input=stdin_prompt,
                    )

                    stdout = result.stdout.strip()
                    stderr = result.stderr.strip()

                    if result.returncode != 0:
                        error_msg = stderr or stdout or f"Claude CLI exited with code {result.returncode}"
                        logger.error("Claude CLI error (code %d): %s", result.returncode, error_msg)
                        return ProviderResult(is_error=True, requires_reply=True, text=error_msg, return_code=result.returncode)

                    # Claude outputs clean JSON with --output-format json
                    try:
                        data = json.loads(stdout)
                    except json.JSONDecodeError:
                        # Fallback for non-JSON output (e.g. fatal errors before JSON emission)
                        if stdout:
                            logger.info("Claude CLI returned non-JSON output: %s", stdout[:200])
                            return ProviderResult(is_error=True, requires_reply=True, text=stdout, return_code=0)
                        logger.info("Claude CLI completed successfully (silent)")
                        return ProviderResult(is_error=False, requires_reply=False, text="", return_code=0)

                    response = data.get("result", "").strip()
                    returned_session_id = data.get("session_id", "")
                    is_error_flag = data.get("is_error", False)

                    # Build stats dict from Claude's usage data
                    stats = {}
                    if data.get("modelUsage"):
                        stats["modelUsage"] = data["modelUsage"]
                    if data.get("total_cost_usd") is not None:
                        stats["total_cost_usd"] = data["total_cost_usd"]
                    if data.get("duration_ms") is not None:
                        stats["duration_ms"] = data["duration_ms"]

                    # Check for error from Claude's own is_error flag
                    if is_error_flag or data.get("subtype", "").startswith("error"):
                        error_msg = response or data.get("subtype", "Unknown Claude error")
                        logger.error("Claude CLI returned error: %s", error_msg[:200])
                        return ProviderResult(is_error=True, requires_reply=True, text=error_msg, return_code=0, session_id=returned_session_id, stats=stats)

                    if response:
                        # Check for success signal code word
                        if response.strip() == "SYNAPSE_OK":
                            logger.info("Claude CLI completed successfully (SYNAPSE_OK)")
                            return ProviderResult(is_error=False, requires_reply=False, text="", return_code=0, session_id=returned_session_id, stats=stats)

                        # Non-empty response = agent wants to relay something (question/answer)
                        logger.info("Claude CLI returned response: %s", response[:200])
                        return ProviderResult(is_error=False, requires_reply=True, text=response, return_code=0, session_id=returned_session_id, stats=stats)

                    # Empty response = silent success
                    logger.info("Claude CLI completed successfully (silent response)")
                    return ProviderResult(is_error=False, requires_reply=False, text="", return_code=0, session_id=returned_session_id, stats=stats)

                except subprocess.TimeoutExpired:
                    logger.error("Claude CLI timed out after %ds", config.CLAUDE_TIMEOUT_SECONDS)
                    return ProviderResult(
                        is_error=True,
                        requires_reply=True,
                        text=f"Claude CLI timed out after {config.CLAUDE_TIMEOUT_SECONDS} seconds.",
                        return_code=-1,
                    )
                except FileNotFoundError:
                    logger.error("Claude CLI not found at '%s'", config.CLAUDE_CMD)
                    return ProviderResult(
                        is_error=True,
                        requires_reply=True,
                        text=f"Claude CLI not found at '{config.CLAUDE_CMD}'. Is it installed and in PATH?",
                        return_code=-1,
                    )
                except Exception as e:
                    logger.error("Unexpected error piping to Claude CLI: %s", e)
                    return ProviderResult(is_error=True, requires_reply=True, text=str(e), return_code=-1)

        # Build the list of models to try
        models_to_try = []
        if model:
            models_to_try.append(model)
        else:
            models_to_try.append(None)

        for m in config.CLAUDE_FALLBACK_MODELS:
            if m not in models_to_try:
                models_to_try.append(m)

        if not auto_retry:
            models_to_try = models_to_try[:1]

        for attempt in range(min(config.CLAUDE_MAX_RETRIES, len(models_to_try))):
            current_model = models_to_try[attempt]

            # Build cmd for this attempt
            cmd = [config.CLAUDE_CMD, "-p", "--output-format", "json", "--dangerously-skip-permissions"]
            if session_id:
                cmd.extend(["--resume", session_id])
            if current_model:
                cmd.extend(["--model", current_model])
            if config.CLAUDE_MAX_BUDGET_USD:
                cmd.extend(["--max-budget-usd", config.CLAUDE_MAX_BUDGET_USD])
            # Use Claude's built-in fallback for automatic overload handling on first attempt
            if attempt == 0 and len(models_to_try) > 1:
                cmd.extend(["--fallback-model", models_to_try[1]])
            # Prompt is piped via stdin to avoid shell parsing issues (e.g. prompts starting with --)

            logger.info("Piping prompt to Claude CLI (vault=%s, session_id=%s, model=%s)",
                        vault_path, session_id or "none", current_model or "default")
            logger.debug("Prompt:\n%s", prompt)

            res = _run_cmd(cmd, stdin_prompt=prompt)

            # Automatic Fallback: If we tried to --resume and it failed due to a session-specific error,
            # strip the --resume flag and try once more.
            # Do NOT drop the session for transient API errors — those should go to model fallback.
            if session_id and res.is_error and "--resume" in " ".join(cmd):
                is_transient = any(s in res.text.lower() for s in ["overloaded", "rate limit", "capacity", "529"])
                if not is_transient:
                    logger.warning("Claude CLI failed with --resume (session issue). Retrying as a fresh session...")
                    fallback_cmd = [config.CLAUDE_CMD, "-p", "--output-format", "json", "--dangerously-skip-permissions"]
                    if current_model:
                        fallback_cmd.extend(["--model", current_model])
                    if config.CLAUDE_MAX_BUDGET_USD:
                        fallback_cmd.extend(["--max-budget-usd", config.CLAUDE_MAX_BUDGET_USD])
                    res = _run_cmd(fallback_cmd, stdin_prompt=prompt)

            if not res.is_error:
                if attempt > 0 and res.requires_reply and res.text:
                    res.text = f"\u26a0\ufe0f Processed using fallback model ({current_model or 'default'}) due to capacity limits.\n\n{res.text}"
                return res

            # Log error and prepare for next retry if applicable
            logger.warning("Attempt %d failed using model %s: %s", attempt + 1, current_model or "default", res.text[:200])

            # On the final attempt, return the error
            if attempt == min(config.CLAUDE_MAX_RETRIES, len(models_to_try)) - 1:
                return res

            # If cleanup is requested, log it (Claude has no session deletion, but clear our tracking)
            if cleanup_on_error and res.session_id:
                logger.info("Cleanup requested for session %s (no-op for Claude)", res.session_id)

        return res

    def cleanup_session(self, session_id: str) -> None:
        """
        Claude CLI does not support session deletion. No-op.
        """
        if not session_id:
            return
        logger.debug("cleanup_session called for Claude (no-op): %s", session_id)
