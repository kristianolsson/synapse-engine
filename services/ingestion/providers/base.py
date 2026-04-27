
from abc import ABC, abstractmethod
from dataclasses import dataclass
import threading
from typing import Optional, List, Dict, Any

GLOBAL_PROVIDER_LOCK = threading.Lock()

@dataclass
class ProviderResult:
    """Result of an AI provider generation."""
    text: str
    is_error: bool
    requires_reply: bool
    stats: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    return_code: int = 0
    provider_name: str = ""

class AIProvider(ABC):
    """Abstract base class for AI providers."""

    @abstractmethod
    def generate_response(self, prompt: str, session_id: Optional[str] = None, attachments: List[str] = [], model: Optional[str] = None, auto_retry: bool = True, cleanup_on_error: bool = False) -> ProviderResult:
        """
        Generate a response from the AI provider.

        Args:
            prompt: The prompt to send.
            session_id: Optional session ID to resume.
            attachments: List of file paths to attach.
            model: Optional explicit model override for this request.
            auto_retry: Whether to retry with fallback models sequentially.
            cleanup_on_error: Whether to automatically delete the generated session on quota failures before retrying.

        Returns:
            ProviderResult object containing the response text and metadata.
        """
        pass
