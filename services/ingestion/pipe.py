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
    # Use --prompt=VALUE syntax to prevent CLI from interpreting "---" as a flag
    # Use --output-format=json to reliably parse the response
    cmd = [config.GEMINI_CMD, f"--prompt={prompt}", "--yolo", "--output-format=json"]
    if session_id:
        cmd.insert(1, "--resume")
        cmd.insert(2, session_id)

    logger.info("Piping prompt to Gemini CLI (vault=%s, session_id=%s)", vault_path, session_id or "none")
    logger.debug("Prompt:\n%s", prompt)

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

    res = _run_cmd(cmd)

    # Automatic Fallback: If we tried to --resume and it failed (e.g., session expired on CLI side),
    # strip the --resume flag and try one more time as a fresh session.
    if session_id and res.is_error and "--resume" in cmd:
        logger.warning("Gemini CLI failed with --resume. Retrying as a fresh session...")
        fallback_cmd = [config.GEMINI_CMD, f"--prompt={prompt}", "--yolo", "--output-format=json"]
        res = _run_cmd(fallback_cmd)

    return res
