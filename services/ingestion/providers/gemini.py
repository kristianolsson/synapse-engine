import logging
import os
import subprocess
import json
import re
import threading
from typing import Optional, List

from .. import config
from .base import AIProvider, ProviderResult

logger = logging.getLogger(__name__)

# Global lock to prevent concurrent Gemini CLI executions across threads
_gemini_lock = threading.Lock()

def _clean_error_message(raw_error: str) -> str:
    """
    Cleans up the raw stdout/stderr from the Gemini CLI.
    If a GaxiosError or QuotaError JSON block is found, it extracts the human-readable message.
    Otherwise, it strips deprecation warnings and internal initialization logs.
    """
    # 1. Look for embedded JSON arrays from GaxiosError (e.g. `GaxiosError: [{...}]`)
    if match := re.search(r"GaxiosError:\s*(\[.*?\])\n\s+at", raw_error, re.DOTALL):
        try:
            error_arr = json.loads(match.group(1))
            if isinstance(error_arr, list) and len(error_arr) > 0:
                first_err = error_arr[0].get("error", {})
                message = first_err.get("message")
                status = first_err.get("status", "UNKNOWN_STATUS")
                if message:
                    return f"{status}: {message}"
        except Exception:
            pass  # Fall back to line stripping if JSON parsing fails

    # 2. Look for the top-level RetryableQuotaError message which is sometimes cleanly printed
    if match := re.search(r"RetryableQuotaError:\s*(.*?)\n", raw_error):
        return f"Quota Error: {match.group(1).strip()}"

    # 3. Fallback: line-by-line cleanup
    clean_lines = []
    lines = raw_error.splitlines()
    skip_next = False
    
    for line in lines:
        if skip_next:
            skip_next = False
            continue
            
        stripped = line.strip()
        # Skip Node deprecation warnings, which often span two lines
        if "[DEP0040]" in line:
            skip_next = True
            continue
        if "YOLO mode is enabled" in line or "Loaded cached credentials" in line:
            continue
        # Stop collecting at the start of a raw Node stack trace to keep it clean
        if stripped.startswith("at ") or stripped.startswith("config: {"):
            continue
        if not stripped:
            continue
            
        clean_lines.append(stripped)
        
    return "\n".join(clean_lines) if clean_lines else raw_error


class GeminiProvider(AIProvider):
    """
    Implementation of Gemini provider using the Gemini CLI.
    """
    
    def generate_response(self, prompt: str, session_id: Optional[str] = None, attachments: List[str] = [], model: Optional[str] = None) -> ProviderResult:
        """
        Execute the Gemini CLI with the given prompt inside the vault directory.
        """
        vault_path = config.VAULT_PATH

        # Build env with the Gemini CLI's directory in PATH (needed for nvm-managed node)
        env = os.environ.copy()
        gemini_dir = os.path.dirname(config.GEMINI_CMD)
        if gemini_dir:
            env["PATH"] = gemini_dir + ":" + env.get("PATH", "")

        def _run_cmd(current_cmd: list[str]) -> ProviderResult:
            logger.debug("Waiting for Gemini CLI lock...")
            with _gemini_lock:
                logger.debug("Acquired Gemini CLI lock.")
                try:
                    result = subprocess.run(
                        current_cmd,
                        cwd=vault_path,
                        capture_output=True,
                        text=True,
                        timeout=config.GEMINI_TIMEOUT_SECONDS,
                        env=env,
                    )

                    stdout = result.stdout.strip()
                    stderr = result.stderr.strip()

                    if result.returncode != 0:
                        error_msg = stderr or stdout or f"Gemini CLI exited with code {result.returncode}"
                        logger.error("Gemini CLI error (code %d): %s", result.returncode, error_msg)
                        return ProviderResult(is_error=True, requires_reply=True, text=error_msg, return_code=result.returncode)

                    if match := re.search(r"(\{.*\})", stdout, re.DOTALL):
                        try:
                            data = json.loads(match.group(1))
                            response = data.get("response", "").strip()
                            returned_session_id = data.get("session_id", "")
                            stats = data.get("stats", None)
                            
                            if response:
                                # Check for success signal code word
                                if response.strip() == "SYNAPSE_OK":
                                    logger.info("Gemini CLI completed successfully (SYNAPSE_OK)")
                                    return ProviderResult(is_error=False, requires_reply=False, text="", return_code=0, session_id=returned_session_id, stats=stats)

                                # Non-empty response = agent wants to relay something (question/error)
                                logger.info("Gemini CLI returned response: %s", response[:200])
                                return ProviderResult(is_error=False, requires_reply=True, text=response, return_code=0, session_id=returned_session_id, stats=stats)
                            
                            # Empty response = silent success
                            logger.info("Gemini CLI completed successfully (silent response)")
                            return ProviderResult(is_error=False, requires_reply=False, text="", return_code=0, session_id=returned_session_id, stats=stats)
                        except json.JSONDecodeError:
                            logger.warning("Failed to parse JSON from stdout despite --output-format=json")
                    
                    # Fallback for non-JSON output (e.g. fatal errors before JSON emission)
                    if stdout:
                        logger.info("Gemini CLI returned non-JSON output: %s", stdout[:200])
                        return ProviderResult(is_error=True, requires_reply=True, text=stdout, return_code=0)

                    # Empty output = success
                    logger.info("Gemini CLI completed successfully (silent)")
                    return ProviderResult(is_error=False, requires_reply=False, text="", return_code=0)

                except subprocess.TimeoutExpired:
                    logger.error("Gemini CLI timed out after %ds", config.GEMINI_TIMEOUT_SECONDS)
                    return ProviderResult(
                        is_error=True,
                        requires_reply=True,
                        text=f"Gemini CLI timed out after {config.GEMINI_TIMEOUT_SECONDS} seconds.",
                        return_code=-1,
                    )
                except FileNotFoundError:
                    logger.error("Gemini CLI not found at '%s'", config.GEMINI_CMD)
                    return ProviderResult(
                        is_error=True,
                        requires_reply=True,
                        text=f"Gemini CLI not found at '{config.GEMINI_CMD}'. Is it installed and in PATH?",
                        return_code=-1,
                    )
                except Exception as e:
                    logger.error("Unexpected error piping to Gemini CLI: %s", e)
                    return ProviderResult(is_error=True, requires_reply=True, text=str(e), return_code=-1)

        # Build the list of models to try
        models_to_try = []
        if model:
            models_to_try.append(model)
        for m in config.GEMINI_FALLBACK_MODELS:
            # If the user explicitly requested a model, don't try it twice immediately
            if m not in models_to_try:
                models_to_try.append(m)
        
        for attempt in range(min(config.GEMINI_MAX_RETRIES, len(models_to_try))):
            current_model = models_to_try[attempt]
            
            # Build cmd for this attempt
            cmd = [config.GEMINI_CMD, f"--prompt={prompt}", "--yolo", "--output-format=json"]
            if session_id:
                cmd.insert(1, "--resume")
                cmd.insert(2, session_id)
            if current_model:
                cmd.append(f"--model={current_model}")
                
            logger.info("Piping prompt to Gemini CLI (vault=%s, session_id=%s, model=%s)", 
                        vault_path, session_id or "none", current_model or "default")
            logger.debug("Prompt:\n%s", prompt)
                
            res = _run_cmd(cmd)
            
            # Automatic Fallback: If we tried to --resume and it failed due to a session-specific error
            # (e.g., session expired/invalid), strip the --resume flag and try once more.
            # Do NOT drop the session for transient API errors (429, quota) — those should go to model fallback.
            if session_id and res.is_error and "--resume" in cmd:
                is_transient = any(s in res.text.lower() for s in ["429", "quota", "rate limit", "capacity", "resource_exhausted"])
                if not is_transient:
                    logger.warning("Gemini CLI failed with --resume (session issue). Retrying as a fresh session...")
                    fallback_cmd = [config.GEMINI_CMD, f"--prompt={prompt}", "--yolo", "--output-format=json"]
                    if current_model:
                        fallback_cmd.append(f"--model={current_model}")
                    res = _run_cmd(fallback_cmd)
            
            if not res.is_error:
                if attempt > 0 and res.requires_reply and res.text:
                    res.text = f"⚠️ Processed using fallback model ({current_model or 'default'}) due to quota limits.\n\n{res.text}"
                return res
                
            # Log error and prepare for next retry if applicable
            error_str = _clean_error_message(res.text)
            logger.warning("Attempt %d failed using model %s: %s", attempt + 1, current_model or "default", error_str)
            
            # On the final attempt, return the cleaned error message
            if attempt == min(config.GEMINI_MAX_RETRIES, len(models_to_try)) - 1:
                res.text = error_str
                return res

        return res

    def cleanup_session(self, session_id: str) -> None:
        """
        Deletes a Gemini CLI session to prevent cluttering the project's session history.
        """
        if not session_id:
            return
            
        vault_path = config.VAULT_PATH
        cmd = [config.GEMINI_CMD, "--delete-session", session_id]

        env = os.environ.copy()
        gemini_dir = os.path.dirname(config.GEMINI_CMD)
        if gemini_dir:
            env["PATH"] = gemini_dir + ":" + env.get("PATH", "")

        logger.debug("Cleaning up Gemini session %s...", session_id)
        try:
            # We use Popen instead of run here so we don't necessarily block the caller,
            # though it executes very quickly anyway.
            subprocess.Popen(
                cmd,
                cwd=vault_path,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            logger.error("Failed to execute session cleanup for %s: %s", session_id, e)
