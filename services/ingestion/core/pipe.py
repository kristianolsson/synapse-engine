"""
Prompt standardization and AI provider pipe.

Wraps incoming messages in a metadata block and pipes them to the configured AI provider.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import config
from ..providers import get_provider, AIProvider
from ..providers.base import GLOBAL_PROVIDER_LOCK

logger = logging.getLogger(__name__)

@dataclass
class IncomingMessage:
    """Standardized representation of an ingested message."""

    source_type: str  # "email" or "telegram"
    sender: str
    subject: str = ""
    body: str = ""
    attachment_paths: list[str] = field(default_factory=list)


def _sync_git() -> str:
    """
    Attempt to run git pull on the vault directory.
    Returns plain status text; the required response to a failure (retry,
    then halt if it still fails) is defined once in the vault's Mandate 4
    (Git Sync), not repeated here.

    Acquires GLOBAL_PROVIDER_LOCK, since a provider session (run under the same
    lock) may run its own git commands against the vault at any point via its
    shell tool. Serializing against that same lock prevents this pull from
    racing another thread's pull or a provider's in-session git usage.
    """
    import subprocess
    logger.debug("Waiting for GLOBAL_PROVIDER_LOCK for git pull...")
    with GLOBAL_PROVIDER_LOCK:
        logger.debug("Acquired GLOBAL_PROVIDER_LOCK for git pull.")
        try:
            result = subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=config.VAULT_PATH,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return "Git Context: OK"
            else:
                err_msg = result.stderr.strip().replace('\n', ' ')[:200]
                logger.warning("Automatic git pull failed: %s", err_msg)
                return f"Git Context: FAILED - {err_msg}"
        except Exception as e:
            logger.warning("Automatic git pull exception: %s", e)
            return f"Git Context: FAILED - {e}"


def sync_and_build_prompt(msg: IncomingMessage) -> str:
    """
    Run a pre-flight git pull against the vault, then wrap the message in the
    metadata block format expected by the Ingestion Protocols defined in
    GEMINI.md. The git sync result is embedded in that metadata block as
    "Git Context: OK/FAILED - ..." — this isn't a pure formatting function.
    """
    from datetime import datetime

    attachment_line = f"Attachments: {len(msg.attachment_paths)} attached" if msg.attachment_paths else "Attachments: none"

    # If there are attachments, append their paths so the CLI can reference them
    attachment_refs = ""
    if msg.attachment_paths:
        refs = "\n".join(f"  - {p}" for p in msg.attachment_paths)
        attachment_refs = (
            f"\n\n**Attached Files (use read_file to analyze):**\n{refs}"
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
        f"{attachment_line}\n"
        f"---\n\n"
        f"{msg.body}"
        f"{attachment_refs}"
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


def pipe_to_provider(prompt: str, session_id: Optional[str] = None, model: Optional[str] = None, auto_retry: bool = True, cleanup_on_error: bool = False, provider_name: Optional[str] = None, extra_env: Optional[dict] = None) -> PipeResult:
    """
    Execute the AI provider with the given prompt.
    Kept as 'pipe_to_provider' for backward compatibility, but now delegates to the configured provider.
    """
    provider: AIProvider = get_provider(provider_name)

    # We don't support attachments in this signature yet, but Provider supports it.
    # Future work: update this signature to accept attachments.
    result = provider.generate_response(prompt, session_id=session_id, attachments=[], model=model, auto_retry=auto_retry, cleanup_on_error=cleanup_on_error, extra_env=extra_env)

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
