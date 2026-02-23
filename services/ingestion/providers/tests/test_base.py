
import pytest
from services.ingestion.providers.base import AIProvider, ProviderResult

def test_ai_provider_abstraction():
    # Verify AIProvider is an abstract base class
    with pytest.raises(TypeError):
        AIProvider()

    # Verify that a subclass must implement generate_response
    class IncompleteProvider(AIProvider):
        pass

    with pytest.raises(TypeError):
        IncompleteProvider()

    class CompleteProvider(AIProvider):
        def generate_response(self, prompt, session_id=None, attachments=[]):
            return ProviderResult(
                text="ok",
                is_error=False,
                requires_reply=False,
                stats=None,
                session_id=session_id
            )

    provider = CompleteProvider()
    res = provider.generate_response("hello")
    assert res.text == "ok"
