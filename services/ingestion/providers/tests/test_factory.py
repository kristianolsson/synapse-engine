import pytest
from services.ingestion.providers import get_provider
from services.ingestion.providers.gemini import GeminiProvider
from services.ingestion.providers.echo import EchoProvider

def test_get_provider_invalid():
    with pytest.raises(ValueError, match="Unknown AI provider"):
        get_provider("non_existent_provider")

def test_get_provider_default(monkeypatch):
    # Mock the configuration to return 'gemini'
    import services.ingestion.config as config
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    
    provider = get_provider()
    assert isinstance(provider, GeminiProvider)

def test_get_provider_explicit():
    provider = get_provider("gemini")
    assert isinstance(provider, GeminiProvider)
    
    provider = get_provider("echo")
    assert isinstance(provider, EchoProvider)
