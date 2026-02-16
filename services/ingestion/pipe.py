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
from dataclasses import dataclass, field

from . import config

logger = logging.getLogger(__name__)


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

    prompt = (
        f"---\n"
        f"Type: {msg.source_type}\n"
        f"Sender: {msg.sender}\n"
        f"Subject: {msg.subject}\n"
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

    success: bool
    output: str  # Empty string on success; error/clarification text otherwise
    return_code: int = 0


def pipe_to_gemini(prompt: str) -> PipeResult:
    """
    Execute the Gemini CLI with the given prompt inside the vault directory.

    Per the Ingestion Persona spec:
    - Empty stdout → success (silent, no reply needed)
    - Non-empty stdout → error or clarification (relay to user)

    Returns a PipeResult with success status and any output.
    """
    vault_path = config.VAULT_PATH
    # Use --prompt=VALUE syntax to prevent CLI from interpreting "---" as a flag
    # Use --output-format=json to reliably parse the response
    cmd = [config.GEMINI_CMD, f"--prompt={prompt}", "--yolo", "--output-format=json"]

    logger.info("Piping prompt to Gemini CLI (vault=%s)", vault_path)
    logger.debug("Prompt:\n%s", prompt)

    # Build env with the Gemini CLI's directory in PATH (needed for nvm-managed node)
    env = os.environ.copy()
    gemini_dir = os.path.dirname(config.GEMINI_CMD)
    if gemini_dir:
        env["PATH"] = gemini_dir + ":" + env.get("PATH", "")

    try:
        result = subprocess.run(
            cmd,
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
            return PipeResult(success=False, output=error_msg, return_code=result.returncode)

        if match := re.search(r"(\{.*\})", stdout, re.DOTALL):
            try:
                data = json.loads(match.group(1))
                response = data.get("response", "").strip()
                
                if response:
                    # Check for success signal code word
                    if response.strip() == "SYNAPSE_OK":
                        logger.info("Gemini CLI completed successfully (SYNAPSE_OK)")
                        return PipeResult(success=True, output="", return_code=0)

                    # Non-empty response = agent wants to relay something (question/error)
                    logger.info("Gemini CLI returned response: %s", response[:200])
                    return PipeResult(success=False, output=response, return_code=0)
                
                # Empty response = success (silent)
                logger.info("Gemini CLI completed successfully (silent response)")
                return PipeResult(success=True, output="", return_code=0)
            except json.JSONDecodeError:
                logger.warning("Failed to parse JSON from stdout despite --output-format=json")
        
        # Fallback for non-JSON output (e.g. fatal errors before JSON emission)
        if stdout:
            logger.info("Gemini CLI returned non-JSON output: %s", stdout[:200])
            return PipeResult(success=False, output=stdout, return_code=0)

        # Empty output = success
        logger.info("Gemini CLI completed successfully (silent)")
        return PipeResult(success=True, output="", return_code=0)

    except subprocess.TimeoutExpired:
        logger.error("Gemini CLI timed out after %ds", config.GEMINI_TIMEOUT_SECONDS)
        return PipeResult(
            success=False,
            output=f"Gemini CLI timed out after {config.GEMINI_TIMEOUT_SECONDS} seconds.",
            return_code=-1,
        )
    except FileNotFoundError:
        logger.error("Gemini CLI not found at '%s'", config.GEMINI_CMD)
        return PipeResult(
            success=False,
            output=f"Gemini CLI not found at '{config.GEMINI_CMD}'. Is it installed and in PATH?",
            return_code=-1,
        )
    except Exception as e:
        logger.error("Unexpected error piping to Gemini CLI: %s", e)
        return PipeResult(success=False, output=str(e), return_code=-1)
