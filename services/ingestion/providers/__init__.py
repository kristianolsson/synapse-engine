
import logging
from typing import Optional
from .base import AIProvider
from .gemini import GeminiProvider
from .claude import ClaudeProvider
from .echo import EchoProvider
from .agy import AgyProvider

logger = logging.getLogger(__name__)

# Single source of truth for valid provider names. get_provider() and the
# /provider command handlers (email + Telegram listeners) all key off this
# instead of independently hardcoded tuples — those already drifted out of
# sync once (AgyProvider was added to Telegram's tuple but not email's).
PROVIDER_REGISTRY: dict = {
    "gemini": GeminiProvider,
    "claude": ClaudeProvider,
    "echo": EchoProvider,
    "agy": AgyProvider,
}

# 'echo' is a test stub, intentionally left out of user-facing provider lists.
_HIDDEN_PROVIDERS = {"echo"}


def user_visible_provider_names() -> list[str]:
    """Provider names to show in /provider help and error text, derived from
    PROVIDER_REGISTRY so they can't drift from the actual valid set."""
    return sorted(name for name in PROVIDER_REGISTRY if name not in _HIDDEN_PROVIDERS)


def get_provider(provider_name: Optional[str] = None) -> AIProvider:
    """
    Get an AI provider instance.

    Args:
        provider_name: Optional override for the provider name.
                       If not provided, uses AI_PROVIDER env var (defaults to 'gemini').

    Returns:
        An instance of AIProvider.

    Raises:
        ValueError: If the provider is unknown.
    """
    if not provider_name:
        from ..config import get_ai_provider
        provider_name = get_ai_provider()

    provider_cls = PROVIDER_REGISTRY.get(provider_name)
    if provider_cls is None:
        raise ValueError(f"Unknown AI provider: {provider_name}")
    return provider_cls()
