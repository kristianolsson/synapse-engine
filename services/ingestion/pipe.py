"""
Prompt standardization and Gemini CLI pipe.

Wraps incoming messages in a metadata block and pipes them to the Gemini CLI
running inside the notes vault. The CLI is the sole agent for file ops and git.
"""

import logging
import os
import subprocess
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from . import config

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


@dataclass
class IncomingMessage:
    """Standardized representation of an ingested message."""

    source_type: str  # "email" or "telegram"
    sender: str
    subject: str = ""
    body: str = ""
    image_paths: list[str] = field(default_factory=list)


def build_prompt(msg: IncomingMessage) -> str:
    """
    Wrap a message in the metadata block format expected by the
    Ingestion Protocols defined in GEMINI.md.
    """
    image_line = f"Images: {len(msg.image_paths)} attached" if msg.image_paths else "Images: none"

    # If there are images, append their paths so the CLI can reference them
    image_refs = ""
    if msg.image_paths:
        refs = "\n".join(f"  - {p}" for p in msg.image_paths)
        image_refs = (
            f"\n\n**Attached Images (use read_file to analyze):**\n{refs}"
        )

    subject_line = f"Subject: {msg.subject}\n" if msg.subject else ""

    prompt = (
        f"---\n"
        f"Type: {msg.source_type}\n"
        f"Sender: {msg.sender}\n"
        f"{subject_line}"
        f"Context: Ingested via {msg.source_type.upper()}\n"
        f"{image_line}\n"
        f"---\n\n"
        f"{msg.body}"
        f"{image_refs}"
    )
    return prompt


@dataclass
class PipeResult:
    """Result of piping a prompt to the Gemini CLI."""

    is_error: bool
    requires_reply: bool
    output: str  # text to relay to user (if requires_reply is True)
    return_code: int = 0
    session_id: str = ""


def pipe_to_gemini(prompt: str, session_id: Optional[str] = None) -> PipeResult:
    """
    Execute the Gemini CLI with the given prompt inside the vault directory.

    Per the Ingestion Persona spec:
    - Empty stdout → silent success (requires_reply=False)
    - Non-empty stdout → error or clarification (requires_reply=True)

    Returns a PipeResult with error status, reply requirement, and output.
    """
    vault_path = config.VAULT_PATH

    # Build env with the Gemini CLI's directory in PATH (needed for nvm-managed node)
    env = os.environ.copy()
    gemini_dir = os.path.dirname(config.GEMINI_CMD)
    if gemini_dir:
        env["PATH"] = gemini_dir + ":" + env.get("PATH", "")

    def _run_cmd(current_cmd: list[str]) -> PipeResult:
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
                    return PipeResult(is_error=True, requires_reply=True, output=error_msg, return_code=result.returncode)

                if match := re.search(r"(\{.*\})", stdout, re.DOTALL):
                    try:
                        data = json.loads(match.group(1))
                        response = data.get("response", "").strip()
                        returned_session_id = data.get("session_id", "")
                        
                        if response:
                            # Check for success signal code word
                            if response.strip() == "SYNAPSE_OK":
                                logger.info("Gemini CLI completed successfully (SYNAPSE_OK)")
                                return PipeResult(is_error=False, requires_reply=False, output="", return_code=0, session_id=returned_session_id)

                            # Non-empty response = agent wants to relay something (question/error)
                            logger.info("Gemini CLI returned response: %s", response[:200])
                            return PipeResult(is_error=False, requires_reply=True, output=response, return_code=0, session_id=returned_session_id)
                        
                        # Empty response = silent success
                        logger.info("Gemini CLI completed successfully (silent response)")
                        return PipeResult(is_error=False, requires_reply=False, output="", return_code=0, session_id=returned_session_id)
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse JSON from stdout despite --output-format=json")
                
                # Fallback for non-JSON output (e.g. fatal errors before JSON emission)
                if stdout:
                    logger.info("Gemini CLI returned non-JSON output: %s", stdout[:200])
                    return PipeResult(is_error=True, requires_reply=True, output=stdout, return_code=0)

                # Empty output = success
                logger.info("Gemini CLI completed successfully (silent)")
                return PipeResult(is_error=False, requires_reply=False, output="", return_code=0)

            except subprocess.TimeoutExpired:
                logger.error("Gemini CLI timed out after %ds", config.GEMINI_TIMEOUT_SECONDS)
                return PipeResult(
                    is_error=True,
                    requires_reply=True,
                    output=f"Gemini CLI timed out after {config.GEMINI_TIMEOUT_SECONDS} seconds.",
                    return_code=-1,
                )
            except FileNotFoundError:
                logger.error("Gemini CLI not found at '%s'", config.GEMINI_CMD)
                return PipeResult(
                    is_error=True,
                    requires_reply=True,
                    output=f"Gemini CLI not found at '{config.GEMINI_CMD}'. Is it installed and in PATH?",
                    return_code=-1,
                )
            except Exception as e:
                logger.error("Unexpected error piping to Gemini CLI: %s", e)
                return PipeResult(is_error=True, requires_reply=True, output=str(e), return_code=-1)

    models_to_try = [None] + config.GEMINI_FALLBACK_MODELS
    
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
        
        # Automatic Fallback: If we tried to --resume and it failed (e.g., session expired on CLI side),
        # strip the --resume flag and try one more time as a fresh session for this specific model attempt.
        if session_id and res.is_error and "--resume" in cmd:
            logger.warning("Gemini CLI failed with --resume. Retrying as a fresh session...")
            fallback_cmd = [config.GEMINI_CMD, f"--prompt={prompt}", "--yolo", "--output-format=json"]
            if current_model:
                fallback_cmd.append(f"--model={current_model}")
            res = _run_cmd(fallback_cmd)
        
        if not res.is_error:
            return res
            
        # Log error and prepare for next retry if applicable
        error_str = _clean_error_message(res.output)
        logger.warning("Attempt %d failed using model %s: %s", attempt + 1, current_model or "default", error_str)
        
        # On the final attempt, return the cleaned error message
        if attempt == min(config.GEMINI_MAX_RETRIES, len(models_to_try)) - 1:
            res.output = error_str
            return res

    return res
