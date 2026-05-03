"""Page-specific scrapers for Amazon Fresh.

Uses CSS selectors from selectors.json via Playwright — no LLM calls here.
LLM is only involved during 'heal' to discover/repair those selectors.

All scrapers return plain Python dicts/lists — no side effects.
"""

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger("amazon-fresh")


# ── Element helpers ────────────────────────────────────────────────────────────

def _extract_price(element) -> Optional[str]:
    """Extract price text, preferring the screen-reader .a-offscreen element."""
    for sel in (".a-price .a-offscreen", "[data-a-color='price'] .a-offscreen"):
        try:
            text = element.locator(sel).first.text_content(timeout=2000)
            if text and text.strip():
                return text.strip()
        except Exception:
            pass
    return None


def _extract_name(element, name_selector: str) -> Optional[str]:
    """Extract product name using the configured name selector."""
    try:
        text = element.locator(name_selector).first.text_content(timeout=2000)
        if text:
            return text.strip()
    except Exception:
        pass
    return None


def _extract_asin(element) -> Optional[str]:
    """Extract ASIN from data-asin attribute — extremely stable Amazon identifier."""
    try:
        asin = element.get_attribute("data-asin")
        if asin and asin.strip():
            return asin.strip()
    except Exception:
        pass
    return None


def _extract_purchase_date(element) -> Optional[str]:
    """Extract 'Purchased Month YYYY' badge text if present.

    Returns the raw badge string (e.g. 'Purchased Apr 2026'), or None.
    Used to surface and sort previously-purchased items in search results.
    """
    try:
        html = element.inner_html(timeout=2000)
        match = re.search(
            r'Purchased\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})',
            html,
            re.IGNORECASE,
        )
        if match:
            return match.group(0)
    except Exception:
        pass
    return None


def _parse_purchase_date(badge: Optional[str]) -> Optional[datetime]:
    """Parse a purchase badge string into a datetime for sorting."""
    if not badge:
        return None
    try:
        match = re.search(
            r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})',
            badge,
            re.IGNORECASE,
        )
        if match:
            return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%b %Y")
    except Exception:
        pass
    return None


def _is_in_stock(element) -> bool:
    """Return False if the element contains out-of-stock indicators."""
    try:
        html = element.inner_html(timeout=2000).lower()
        return not any(p in html for p in ["out of stock", "currently unavailable", "soldout"])
    except Exception:
        return True  # Assume in stock if we can't tell


# ── Page scrapers ──────────────────────────────────────────────────────────────

def scrape_items(page, sel: dict, limit: Optional[int] = None) -> list:
    """Scrape product items from past-purchases or saved-items pages.

    Args:
        page: Playwright page (already navigated).
        sel: Page selectors dict from selectors.json.
        limit: Max items to return (None = all).

    Returns:
        List of {name, price, asin} dicts.
    """
    container_sel = sel["item_container"]
    name_sel = sel["item_name"]

    try:
        page.wait_for_selector(container_sel, timeout=15000)
    except Exception:
        logger.warning(
            "Item container '%s' not found — selectors may need healing. "
            "Run: amazon-fresh heal", container_sel
        )
        return []

    elements = page.locator(container_sel).all()
    if limit:
        elements = elements[:limit]

    items = []
    for el in elements:
        name = _extract_name(el, name_sel)
        if not name:
            continue  # Skip ads / empty placeholders

        items.append({
            "name": name,
            "price": _extract_price(el),
            "asin": _extract_asin(el),
        })

    return items


def scrape_search_results(page, sel: dict, limit: Optional[int] = None) -> list:
    """Scrape search results, sorting previously-purchased items first.

    Args:
        page: Playwright page (already navigated to search URL).
        sel: Page selectors dict for 'search'.
        limit: Max results to return.

    Returns:
        List of {name, price, asin, in_stock, purchased} dicts,
        sorted by most recent purchase date first, then unpurchased items.
    """
    container_sel = sel["item_container"]
    name_sel = sel["item_name"]

    try:
        page.wait_for_selector(container_sel, timeout=15000)
    except Exception:
        logger.warning(
            "Search container '%s' not found — run: amazon-fresh heal --page search",
            container_sel
        )
        return []

    elements = page.locator(container_sel).all()
    if limit:
        elements = elements[:limit]

    results = []
    for el in elements:
        name = _extract_name(el, name_sel)
        if not name:
            continue

        purchased = _extract_purchase_date(el)
        results.append({
            "name": name,
            "price": _extract_price(el),
            "asin": _extract_asin(el),
            "in_stock": _is_in_stock(el),
            "purchased": purchased,  # e.g. "Purchased Apr 2026" or null
        })

    # Sort: most recently purchased first, then items without a badge
    def _sort_key(r):
        dt = _parse_purchase_date(r.get("purchased"))
        return (0 if dt else 1, -(dt.timestamp() if dt else 0))

    results.sort(key=_sort_key)
    return results


def dump_dom_for_heal(page, max_chars: int = 60000) -> str:
    """Dump a representative portion of the page HTML for LLM selector discovery.

    Called only by 'heal' — not used in normal scraping operations.
    """
    for selector in ["#gridlayout-main-grid", "#search", "#a-page", "main"]:
        try:
            el = page.locator(selector).first
            html = el.inner_html(timeout=3000)
            if html and len(html) > 500:
                break
        except Exception:
            html = None

    if not html:
        html = page.content()

    if len(html) > max_chars:
        html = html[:max_chars] + "\n<!-- TRUNCATED -->"

    return html


def scroll_to_load(page, max_scrolls: int = 8) -> None:
    """Scroll the page to trigger lazy-loading of product items."""
    for _ in range(max_scrolls):
        page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
        page.wait_for_timeout(700)
