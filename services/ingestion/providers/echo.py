
from typing import Optional, List
from .base import AIProvider, ProviderResult

class EchoProvider(AIProvider):
    """
    A dummy provider that simply echoes the prompt.
    Useful for testing and verification without calling external APIs.
    """
    
    def generate_response(self, prompt: str, session_id: Optional[str] = None, attachments: List[str] = []) -> ProviderResult:
        response_text = f"Echo: {prompt}"
        if attachments:
            response_text += "\\nAttachments: " + ", ".join(attachments)
        
        return ProviderResult(
            text=response_text,
            is_error=False,
            requires_reply=True,  # Always reply so we can see the output
            session_id=session_id or "echo-session"
        )
