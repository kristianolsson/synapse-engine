
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any

@dataclass
class ProviderResult:
    """Result of an AI provider generation."""
    text: str
    is_error: bool
    requires_reply: bool
    stats: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    return_code: int = 0

class AIProvider(ABC):
    """Abstract base class for AI providers."""

    @abstractmethod
    def generate_response(self, prompt: str, session_id: Optional[str] = None, attachments: List[str] = [], model: Optional[str] = None) -> ProviderResult:
        """
        Generate a response from the AI provider.

        Args:
            prompt: The prompt to send.
            session_id: Optional session ID to resume.
            attachments: List of file paths to attach.
            model: Optional explicit model override for this request.

        Returns:
            ProviderResult object containing the response text and metadata.
        """
        pass
