"""
Tests for HTML sanitization utility.
"""

from services.ingestion.utils.html_utils import sanitize_telegram_html

def test_sanitize_telegram_html_basic():
    """Verify that supported tags are preserved."""
    html = "Hello <b>world</b> <i>italic</i> <a href='https://example.com'>link</a>"
    sanitized = sanitize_telegram_html(html)
    assert sanitized == html

def test_sanitize_telegram_html_lists():
    """Verify that <ul> and <li> are correctly converted."""
    html = "Your tasks:<ul><li>Task 1</li><li>Task 2</li></ul>"
    # Note: our sanitizer removes <ul> and converts <li> to '• ' and </li> to '\n'
    # Result should be "Your tasks:\n• Task 1\n• Task 2" (after cleanup)
    sanitized = sanitize_telegram_html(html)
    assert "• Task 1" in sanitized
    assert "• Task 2" in sanitized
    assert "<ul>" not in sanitized
    assert "<li>" not in sanitized

def test_sanitize_telegram_html_unsupported_tags():
    """Verify that unsupported tags are stripped but content is kept."""
    html = "<div><p>Header</p><h1>Title</h1><script>alert(1)</script>Safe <b>text</b></div>"
    sanitized = sanitize_telegram_html(html)
    assert "Header" in sanitized
    assert "Title" in sanitized
    assert "Safe" in sanitized
    assert "<b>text</b>" in sanitized
    assert "<div>" not in sanitized
    assert "<p>" not in sanitized
    assert "<h1>" not in sanitized
    assert "<script>" not in sanitized

def test_sanitize_telegram_html_br():
    """Verify that <br> is converted to newline."""
    html = "Line 1<br>Line 2"
    sanitized = sanitize_telegram_html(html)
    assert sanitized == "Line 1\nLine 2"

def test_sanitize_telegram_html_multiline_cleanup():
    """Verify that multiple newlines are collapsed."""
    html = "Line 1<p></p><br><br>Line 2"
    sanitized = sanitize_telegram_html(html)
    assert sanitized == "Line 1\n\nLine 2"

def test_sanitize_telegram_html_empty():
    """Verify empty input handling."""
    assert sanitize_telegram_html("") == ""
    assert sanitize_telegram_html(None) == ""
