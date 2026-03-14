"""
HTML utility functions for message sanitization across different channels.
"""

import re
import logging
from typing import Set

logger = logging.getLogger(__name__)

# Official Telegram-supported HTML tags
# Source: https://core.telegram.org/bots/api#html-style
TELEGRAM_SUPPORTED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-emoji", "a", "code", "pre", "blockquote"
}

def sanitize_telegram_html(html: str) -> str:
    """
    Sanitize HTML for Telegram's restricted HTML parse mode.
    
    - Replaces <ul> and </ul> with newlines (or empty string if redundant).
    - Replaces <li> with '• ' and </li> with a newline.
    - Strips all unsupported tags while preserving their inner content.
    - Preserves supported tags (b, i, u, s, a, code, pre, blockquote, etc).
    
    Args:
        html: The raw HTML string to sanitize.
        
    Returns:
        A sanitized string compatible with Telegram.
    """
    if not html:
        return ""

    # 1. Handle Lists (recursive or nested lists aren't fully supported by this simple regex
    # but likely sufficient for Gemini common outputs).
    # Replace <li> with bullets and </li> with newlines
    html = re.sub(r'<li>', '• ', html, flags=re.IGNORECASE)
    html = re.sub(r'</li>', '\n', html, flags=re.IGNORECASE)
    
    # Remove <ul> tags entirely
    html = re.sub(r'</?ul>', '', html, flags=re.IGNORECASE)
    
    # 2. Strip unsupported tags but keep content
    # This regex finds all <tag ...> and </tag>
    tag_pattern = re.compile(r'<(/?)([a-z0-9-]+)([^>]*)>', re.IGNORECASE)
    
    def tag_replacer(match):
        is_closing = match.group(1) == "/"
        tag_name = match.group(2).lower()
        full_tag = match.group(0)
        
        if tag_name in TELEGRAM_SUPPORTED_TAGS:
            return full_tag
        
        # If it's a block-level tag we're stripping, maybe add a newline?
        # For now, just strip the tag and keep the text.
        if tag_name in {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "br"}:
            if tag_name == "br":
                return "\n"
            # Just keep the text for other block tags
            return ""
            
        return ""

    sanitized = tag_pattern.sub(tag_replacer, html)
    
    # Clean up multiple newlines that might have been created
    sanitized = re.sub(r'\n{3,}', '\n\n', sanitized).strip()
    
    return sanitized
