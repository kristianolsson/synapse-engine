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
    """Extract ASIN from data-asin attribute, element id, or product link."""
    try:
        asin = element.get_attribute("data-asin")
        if asin and asin.strip():
            return asin.strip()
    except Exception:
        pass

    # Past-purchases cards use id="closedCard-<ASIN>" — pull it from there directly.
    try:
        elem_id = element.get_attribute("id") or ""
        match = re.search(r'([A-Z0-9]{10})$', elem_id)
        if match:
            return match.group(1)
    except Exception:
        pass

    # Fallback: look for /dp/ or /gp/product/ URLs anywhere in the container HTML.
    try:
        html = element.inner_html(timeout=2000)
        match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', html)
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


# ── Cart actions ──────────────────────────────────────────────────────────────

def _card_name(card) -> str:
    """Extract product name from a search result card, or empty string."""
    try:
        text = card.locator("h2 span").first.text_content(timeout=3000)
        return text.strip() if text else ""
    except Exception:
        return ""


def add_to_cart(page, asin: str, qty: int, sel: dict) -> dict:
    """Add an item to the Amazon Fresh cart.

    The page must already be navigated to the Fresh search URL for this ASIN
    (caller handles navigation so auth check happens before we get here).
    Uses selectors from selectors.json["add"] for all button interactions.

    The search may return many unrelated items — we scope the item_container
    selector to the specific data-asin so we always click the right card.

    Returns {"asin", "name", "qty", "added": True}.
    Raises ValueError if the ASIN isn't found or the item is out of stock.
    Raises RuntimeError if the Add to cart button can't be clicked.
    """
    # Scope the configured container selector to the specific ASIN.
    # data-asin on search result cards is stable regardless of result ordering.
    card_sel = f'{sel["item_container"]}[data-asin="{asin}"]'
    try:
        page.wait_for_selector(card_sel, timeout=10000)
    except Exception:
        raise ValueError(
            f"ASIN {asin} not found in Amazon Fresh — "
            "item may not be available in your delivery area"
        )

    card = page.locator(card_sel).first
    name = _card_name(card)
    label = f'"{name}" ' if name else ""

    # Check for explicit out-of-stock indicator if a selector is configured
    oos_sel = sel.get("out_of_stock_indicator")
    if oos_sel:
        try:
            if card.locator(oos_sel).count() > 0:
                raise ValueError(f"{label}({asin}) is out of stock on Amazon Fresh")
        except ValueError:
            raise
        except Exception:
            pass

    add_btn_sel = sel["add_to_cart_button"]

    # If no Add to cart button is present the item is likely out of stock / unavailable
    try:
        if card.locator(add_btn_sel).count() == 0:
            raise ValueError(f"{label}({asin}) is out of stock or unavailable on Amazon Fresh")
    except ValueError:
        raise
    except Exception:
        pass

    try:
        card.locator(add_btn_sel).first.click(timeout=6000)
    except Exception as e:
        raise RuntimeError(
            f"Could not click 'Add to cart' for ASIN {asin}: {e}. "
            "Run 'amazon-fresh heal --page add' to repair selectors."
        )

    page.wait_for_timeout(1500)

    # After the first add, Fresh replaces the button with a +/- stepper inline
    # in the card. Increment (qty-1) times to reach the desired quantity.
    actual_qty = 1
    inc_sel = sel.get("qty_increment_button", "")
    if qty > 1 and inc_sel:
        for _ in range(qty - 1):
            try:
                card.locator(inc_sel).first.click(timeout=3000)
                page.wait_for_timeout(400)
                actual_qty += 1
            except Exception:
                logger.warning(
                    "Could not increment qty for ASIN %s — added %d instead of %d",
                    asin, actual_qty, qty,
                )
                break

    return {"asin": asin, "name": name, "qty": actual_qty, "added": True}


# ── Cart read/write ────────────────────────────────────────────────────────────

def _find_cart_item(page, asin: str, sel: dict):
    """Return the cart item element for the given ASIN, or None.

    Tries a direct data-asin attribute match first (fast), then falls back to
    iterating all containers and extracting the ASIN from each (robust against
    carts where data-asin lives on an inner element instead of the container).
    """
    container_sel = sel["item_container"]

    direct = f'{container_sel}[data-asin="{asin}"]'
    try:
        if page.locator(direct).count() > 0:
            return page.locator(direct).first
    except Exception:
        pass

    try:
        for el in page.locator(container_sel).all():
            if _extract_asin(el) == asin:
                return el
    except Exception:
        pass

    return None


def scrape_cart_items(page, sel: dict) -> dict:
    """Scrape all items from the Amazon Fresh cart page.

    Returns {"items": [{name, asin, qty, price}], "subtotal": str|None}.
    An empty cart returns {"items": [], "subtotal": None} — not an error.
    """
    container_sel = sel["item_container"]
    try:
        page.wait_for_selector(container_sel, timeout=8000)
    except Exception:
        return {"items": [], "subtotal": None}

    items = []
    for el in page.locator(container_sel).all():
        name = _extract_name(el, sel["item_name"])
        if not name:
            continue

        asin = _extract_asin(el)
        price = _extract_price(el, sel["item_price"])

        qty = None
        qty_sel = sel.get("item_qty", "")
        if qty_sel:
            try:
                qty_el = el.locator(qty_sel).first
                # Try input_value first (text inputs / selects), fall back to
                # text_content for combobox buttons which display the current qty
                try:
                    val = qty_el.input_value(timeout=2000)
                except Exception:
                    val = qty_el.text_content(timeout=2000)
                if val:
                    m = re.search(r'\d+', val.strip())
                    qty = int(m.group()) if m else None
            except Exception:
                pass

        items.append({"name": name, "asin": asin, "qty": qty, "price": price})

    subtotal = None
    subtotal_sel = sel.get("subtotal")
    if subtotal_sel:
        try:
            text = page.locator(subtotal_sel).first.text_content(timeout=2000)
            if text:
                subtotal = text.strip()
        except Exception:
            pass

    return {"items": items, "subtotal": subtotal}


def remove_from_cart(page, asin: str, sel: dict) -> dict:
    """Remove an item from the Amazon Fresh cart by ASIN.

    Returns {"asin", "name", "removed": True}.
    Raises ValueError if the ASIN is not in the cart.
    Raises RuntimeError if the delete button can't be clicked.
    """
    card = _find_cart_item(page, asin, sel)
    if card is None:
        raise ValueError(f"ASIN {asin} not found in cart")

    name = _extract_name(card, sel["item_name"]) or asin

    try:
        card.locator(sel["delete_button"]).first.click(timeout=6000)
    except Exception as e:
        raise RuntimeError(
            f"Could not click delete for ASIN {asin}: {e}. "
            "Run 'amazon-fresh heal --page cart' to repair selectors."
        )

    page.wait_for_timeout(2000)
    return {"asin": asin, "name": name, "removed": True}


def edit_cart_qty(page, asin: str, qty: int, sel: dict) -> dict:
    """Update the quantity of an item in the Amazon Fresh cart.

    Tries a direct qty text input first (works for any target quantity),
    then falls back to +/- stepper clicks.

    Returns {"asin", "name", "qty", "updated": True}.
    Raises ValueError if the ASIN is not in the cart.
    Raises RuntimeError if quantity cannot be updated.
    """
    card = _find_cart_item(page, asin, sel)
    if card is None:
        raise ValueError(f"ASIN {asin} not found in cart — use 'add' to add new items")

    name = _extract_name(card, sel["item_name"]) or asin

    # Strategy 1: direct qty input (fastest, works for any number)
    qty_input_sel = sel.get("qty_input", "")
    if qty_input_sel:
        try:
            inp = card.locator(qty_input_sel).first
            inp.triple_click(timeout=3000)
            inp.type(str(qty))
            inp.press("Enter")
            page.wait_for_timeout(1500)
            return {"asin": asin, "name": name, "qty": qty, "updated": True}
        except Exception:
            pass

    # Strategy 2: stepper — read current qty then increment/decrement to target
    current_qty = 1
    item_qty_sel = sel.get("item_qty", "")
    if item_qty_sel:
        try:
            val = card.locator(item_qty_sel).first.input_value(timeout=2000)
            if val and val.strip().isdigit():
                current_qty = int(val)
        except Exception:
            pass

    delta = qty - current_qty
    if delta == 0:
        return {"asin": asin, "name": name, "qty": qty, "updated": True}

    btn_sel = sel.get("qty_increment_button" if delta > 0 else "qty_decrement_button", "")
    if not btn_sel:
        raise RuntimeError(
            f"Cannot update qty for ASIN {asin} — no input or stepper selector available. "
            "Run 'amazon-fresh heal --page cart'."
        )

    for _ in range(abs(delta)):
        try:
            card.locator(btn_sel).first.click(timeout=3000)
            page.wait_for_timeout(400)
        except Exception:
            break

    return {"asin": asin, "name": name, "qty": qty, "updated": True}


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


def dump_dom_for_heal(page, max_chars: int = 300000, scope_selector: str = None, scope_limit: int = 3) -> str:
    """Dump a representative portion of the page HTML for LLM selector discovery.

    Called only by 'heal' — not used in normal scraping operations.

    Args:
        scope_selector: If set, extract only the outerHTML of the first
            `scope_limit` matching elements instead of the whole page.
            Use this for pages where the LLM only needs to see a few
            representative cards (e.g. search results for the add page).
        scope_limit: How many matching elements to include when scoping.
    """
    if scope_selector:
        # Extract just the outerHTML of the first N matching elements.
        # Much cheaper than stripping the full page when we only care about
        # a specific widget's structure.
        fragments = []
        try:
            elements = page.locator(scope_selector).all()[:scope_limit]
            for el in elements:
                try:
                    fragments.append(el.evaluate("el => el.outerHTML"))
                except Exception:
                    pass
        except Exception:
            pass

        if fragments:
            html = "\n".join(fragments)
            # Still strip long data-* attributes
            html = re.sub(r'\bdata-[a-zA-Z0-9_-]+=[\'"][^\'"]{100,}[\'"]', '', html)
            if len(html) > max_chars:
                html = html[:max_chars] + "\n<!-- TRUNCATED -->"
            return html
        # Fall through to full-page extraction if scoping found nothing

    # Grab the full HTML natively
    html = page.content()

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

    if len(html) > max_chars:
        html = html[:max_chars] + "\n<!-- TRUNCATED -->"

    return html


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
