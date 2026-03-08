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
    """Result of piping a prompt to the AI provider."""

    is_error: bool
    requires_reply: bool
    output: str  # text to relay to user (if requires_reply is True)
    return_code: int = 0
    session_id: str = ""
    stats: Optional[dict] = None


def pipe_to_gemini(prompt: str, session_id: Optional[str] = None) -> PipeResult:
    """
    Execute the AI provider with the given prompt.
    Kept as 'pipe_to_gemini' for backward compatibility, but now delegates to the configured provider.
    """
    provider: AIProvider = get_provider()

    # We don't support attachments in this signature yet, but Provider supports it.
    # Future work: update this signature to accept attachments.
    result = provider.generate_response(prompt, session_id=session_id)

    return PipeResult(
        is_error=result.is_error,
        requires_reply=result.requires_reply,
        output=result.text,
        return_code=result.return_code,
        session_id=result.session_id or "",
        stats=result.stats
    )

def cleanup_session(session_id: str) -> None:
    """
    Instruct the active AI provider to delete a session.
    """
    provider: AIProvider = get_provider()
    provider.cleanup_session(session_id)
