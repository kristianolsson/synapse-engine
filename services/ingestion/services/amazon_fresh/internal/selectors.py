"""Selector management for Amazon Fresh CLI.

Loads CSS selectors from selectors.json. These are used directly by Playwright
for scraping — no LLM calls during normal operation.

The 'heal' command is the only place that calls an LLM: it dumps the live DOM,
asks the LLM to identify correct selectors, and rewrites this file.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("amazon-fresh")

SELECTORS_FILE = Path(__file__).parent / "selectors.json"

_SCRAPE_KEYS = ["item_container", "item_name", "item_price"]

REQUIRED_KEYS_BY_PAGE = {
    "past_purchases": _SCRAPE_KEYS,
    "saved_items": _SCRAPE_KEYS,
    "search": _SCRAPE_KEYS,
    "add": ["item_container", "add_to_cart_button"],
    "cart": ["item_container", "item_name", "item_price", "delete_button"],
}


def load_selectors() -> dict:
    """Load the full selectors config from selectors.json."""
    if not SELECTORS_FILE.exists():
        return {}
    try:
        with open(SELECTORS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Failed to load selectors.json: %s", e)
        return {}


def get_page_selectors(page_key: str) -> dict:
    """Return selectors for a specific page.

    Args:
        page_key: One of 'past_purchases', 'saved_items', 'search'.

    Raises:
        KeyError: If selectors are missing or incomplete. Points user to 'heal'.
    """
    selectors = load_selectors()
    if page_key not in selectors:
        raise KeyError(
            f"No selectors found for '{page_key}'. "
            f"Run: amazon-fresh heal --page {page_key.replace('_', '-')}"
        )
    page = selectors[page_key]
    required = REQUIRED_KEYS_BY_PAGE.get(page_key, _SCRAPE_KEYS)
    missing = [k for k in required if k not in page]
    if missing:
        raise KeyError(
            f"Selectors for '{page_key}' incomplete (missing: {missing}). "
            f"Run: amazon-fresh heal --page {page_key.replace('_', '-')}"
        )
    return page


def save_selectors(selectors: dict) -> None:
    """Write updated selectors back to selectors.json."""
    with open(SELECTORS_FILE, "w") as f:
        json.dump(selectors, f, indent=2)
    logger.info("Saved selectors to %s", SELECTORS_FILE)


def merge_page_selectors(page_key: str, new_selectors: dict) -> None:
    """Merge LLM-discovered selectors for a page into selectors.json.

    Preserves the existing URL/url_template. Only updates selector keys.
    """
    all_selectors = load_selectors()
    existing = all_selectors.get(page_key, {})

    # Preserve all existing config (url, url_template, click_selector, etc)
    merged = existing.copy()

    # Merge in new selector keys
    _PRESERVE = {"url", "url_template", "search_url_template", "click_selector"}
    for k, v in new_selectors.items():
        if k not in _PRESERVE and v:
            merged[k] = v

    all_selectors[page_key] = merged
    save_selectors(all_selectors)
