
import os
import logging
from typing import Optional
from .base import AIProvider
from .gemini import GeminiProvider
from .echo import EchoProvider

logger = logging.getLogger(__name__)

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
        from ..config import AI_PROVIDER
        provider_name = AI_PROVIDER.lower()

    if provider_name == "gemini":
        return GeminiProvider()
    if provider_name == "echo":
        return EchoProvider()
    
    raise ValueError(f"Unknown AI provider: {provider_name}")
