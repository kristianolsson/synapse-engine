
from services.ingestion.core.pipe import IncomingMessage, build_prompt, pipe_to_provider

def test_echo_integration_pipeline(monkeypatch):
    # Setup
    import services.ingestion.config as config
    config.set_ai_provider("echo")
    
    # Ingest
    msg = IncomingMessage(
        source_type="telegram",
        sender="999",
        body="Do something"
    )
    
    # Format
    prompt = build_prompt(msg)
    
    # Pipe
    result = pipe_to_provider(prompt)
    
    # Verify
    assert result.is_error is False
    assert result.requires_reply is True
    assert "Echo:" in result.output
    assert "Do something" in result.output
    assert result.session_id == "echo-session"
