from services.ingestion.providers.echo import EchoProvider

def test_echo_provider():
    provider = EchoProvider()

    # Test simple echo
    res = provider.generate_response("Hello")
    assert res.text == "Echo: Hello"
    assert res.is_error is False
    assert res.requires_reply is True
    assert res.session_id == "echo-session"

    # Test with attachments
    res = provider.generate_response("With files", attachments=["file1.txt"])
    assert "Echo: With files" in res.text
    assert "Attachments: file1.txt" in res.text

def test_generate_response_accepts_extra_env():
    provider = EchoProvider()
    result = provider.generate_response("test", extra_env={"SYNAPSE_SESSION_KEY": "user-1"})
    assert result.text == "Echo: test"
