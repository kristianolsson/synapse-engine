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

def _extract_price(element, price_selector: str) -> Optional[str]:
    """Extract price text using the selector from selectors.json."""
    try:
        text = element.locator(price_selector).first.text_content(timeout=2000)
        if text and text.strip():
            return text.strip()
    except Exception:
        pass
        
    # If price is missing, check if it's explicitly out of stock
    try:
        html = element.inner_html(timeout=2000).lower()
        if any(p in html for p in ["out of stock", "currently unavailable", "soldout"]):
            return "Out of Stock"
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
    """Extract ASIN from data-asin attribute, or product link if missing."""
    try:
        asin = element.get_attribute("data-asin")
        if asin and asin.strip():
            return asin.strip()
    except Exception:
        pass

    # Fallback: look for an href containing /dp/ anywhere in the container
    try:
        html = element.inner_html(timeout=2000)
        import re
        match = re.search(r'/dp/([A-Z0-9]{10})', html)
        if match:
            return match.group(1)
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
            "price": _extract_price(el, sel["item_price"]),
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
            "price": _extract_price(el, sel["item_price"]),
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


def dump_dom_for_heal(page, max_chars: int = 300000) -> str:
    """Dump a representative portion of the page HTML for LLM selector discovery.

    Called only by 'heal' — not used in normal scraping operations.
    """
    # Write the raw DOM to a file for debugging
    try:
        with open("debug_dom.html", "w") as f:
            f.write(page.content())
    except Exception:
        pass

    # Grab the full HTML natively
    html = page.content()
    original_len = len(html)

    # Bulletproof native Python stripping (JS evaluate often crashes on Amazon's weird DOM)
    import re

    # 1. Strip massive non-structural tags entirely (along with their inner content)
    for tag in ["script", "style", "svg", "noscript", "meta", "link", "iframe", "header", "footer", "nav"]:
        html = re.sub(rf'<{tag}\b[^>]*>.*?</{tag}>', '', html, flags=re.IGNORECASE | re.DOTALL)
        # Handle self-closing versions just in case
        html = re.sub(rf'<{tag}\b[^>]*/>', '', html, flags=re.IGNORECASE)

    # 2. Strip known Amazon massive header/footer divs
    for a_id in ["navbar", "nav-belt", "nav-main", "navFooter"]:
        html = re.sub(rf'<div[^>]*id=[\'"]{a_id}[\'"][^>]*>.*?</div>', '', html, flags=re.IGNORECASE | re.DOTALL)

    # 3. Strip all massive data-* attributes to save tokens
    html = re.sub(r'\bdata-[a-zA-Z0-9_-]+=[\'"][^\'"]{100,}[\'"]', '', html)

    stripped_len = len(html)

    if len(html) > max_chars:
        html = html[:max_chars] + "\n<!-- TRUNCATED -->"

    return html, original_len, stripped_len


def scroll_to_load(page, item_selector: str = None, max_items: int = 150, max_scrolls: int = 25) -> None:
    """Scroll the page to trigger lazy-loading until item count stabilizes.

    Stops early when a scroll produces no new items (page fully loaded),
    or when max_items or max_scrolls is reached.

    Args:
        page: Playwright page.
        item_selector: CSS selector for the item container. If None, does a fast blind scroll (for heal).
        max_items: Stop scrolling once this many items are visible.
        max_scrolls: Hard limit on scroll attempts regardless of item count.
    """
    if not item_selector:
        # Slow blind scrolls to trigger initial lazy-loaders for LLM heal
        for _ in range(8):
            page.evaluate("window.scrollBy(0, window.innerHeight * 0.5)")
            page.wait_for_timeout(800)
        return

    previous_count = 0
    for _ in range(max_scrolls):
        try:
            # Scroll the last loaded item into view to cleanly trigger the next batch
            items = page.locator(item_selector)
            count = items.count()
            if count > 0:
                items.nth(count - 1).scroll_into_view_if_needed()
            else:
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.3)")
        except Exception:
            page.evaluate("window.scrollBy(0, window.innerHeight * 0.3)")

        page.wait_for_timeout(1500)  # Give lazy-load more time to fire

        try:
            current_count = page.locator(item_selector).count()
        except Exception:
            current_count = 0

        if current_count >= max_items:
            break
            
        if current_count > 0 and current_count == previous_count:
            # Try a small nudge scroll in case we missed the intersection observer
            page.evaluate("window.scrollBy(0, 400)")
            page.wait_for_timeout(1500)
            current_count = page.locator(item_selector).count()
            if current_count == previous_count:
                break  # Definitely fully loaded
                
        previous_count = current_count
