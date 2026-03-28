"""
Prompt standardization and AI provider pipe.

Wraps incoming messages in a metadata block and pipes them to the configured AI provider.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import config
from ..providers import get_provider, AIProvider

logger = logging.getLogger(__name__)

@dataclass
class IncomingMessage:
    """Standardized representation of an ingested message."""

    source_type: str  # "email" or "telegram"
    sender: str
    subject: str = ""
    body: str = ""
    image_paths: list[str] = field(default_factory=list)


def _sync_git() -> str:
    """
    Attempt to run git pull on the vault directory.
    If it fails, return an instruction for the LLM to handle it manually.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=config.VAULT_PATH,
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return "Git Context: Automatically synced and up to date"
        else:
            return "Git Context: AUTOMATIC PULL FAILED. Always execute `git pull` at the start of a session or before reading files to ensure the local context matches the remote repository."
    except Exception:
        return "Git Context: AUTOMATIC PULL FAILED. Always execute `git pull` at the start of a session or before reading files to ensure the local context matches the remote repository."


def build_prompt(msg: IncomingMessage) -> str:
    """
    Wrap a message in the metadata block format expected by the
    Ingestion Protocols defined in GEMINI.md.
    """
    from datetime import datetime

    image_line = f"Images: {len(msg.image_paths)} attached" if msg.image_paths else "Images: none"

    # If there are images, append their paths so the CLI can reference them
    image_refs = ""
    if msg.image_paths:
        refs = "\n".join(f"  - {p}" for p in msg.image_paths)
        image_refs = (
            f"\n\n**Attached Images (use read_file to analyze):**\n{refs}"
        )

    subject_line = f"Subject: {msg.subject}\n" if msg.subject else ""
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S %Z")
    git_status = _sync_git()

    prompt = (
        f"---\n"
        f"Type: {msg.source_type}\n"
        f"Sender: {msg.sender}\n"
        f"{subject_line}"
        f"Context: Ingested via {msg.source_type.upper()}\n"
        f"Current Time: {now}\n"
        f"{git_status}\n"
        f"{image_line}\n"
        f"---\n\n"
        f"{msg.body}"
        f"{image_refs}"
    )
    return prompt


@dataclass
class PipeResult:
    """Result of piping a prompt to the AI provider."""

    is_error: bool
    requires_reply: bool
    output: str  # text to relay to user (if requires_reply is True)
    return_code: int = 0
    session_id: str = ""
    stats: Optional[dict] = None
    provider_name: str = ""


def pipe_to_provider(prompt: str, session_id: Optional[str] = None, model: Optional[str] = None, auto_retry: bool = True, cleanup_on_error: bool = False, provider_name: Optional[str] = None) -> PipeResult:
    """
    Execute the AI provider with the given prompt.
    Kept as 'pipe_to_provider' for backward compatibility, but now delegates to the configured provider.
    """
    provider: AIProvider = get_provider(provider_name)

    # We don't support attachments in this signature yet, but Provider supports it.
    # Future work: update this signature to accept attachments.
    result = provider.generate_response(prompt, session_id=session_id, attachments=[], model=model, auto_retry=auto_retry, cleanup_on_error=cleanup_on_error)

    return PipeResult(
        is_error=result.is_error,
        requires_reply=result.requires_reply,
        output=result.text,
        return_code=result.return_code,
        session_id=result.session_id or "",
        stats=result.stats,
        provider_name=result.provider_name,
    )

def cleanup_session(session_id: str) -> None:
    """
    Instruct the active AI provider to delete a session.
    """
    provider: AIProvider = get_provider()
    provider.cleanup_session(session_id)
