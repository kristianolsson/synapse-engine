#!/usr/bin/env python3
"""Amazon Fresh CLI — browse and manage Amazon Fresh via Playwright Firefox.

This CLI is a reusable tool for the Synapse ecosystem. All output is JSON to stdout.
On error, outputs {"error": "<message>", "code": "<error_type>"} and exits non-zero.

Error codes:
  auth_expired   — Amazon session expired (session needs renewal)
  auth_failed    — Could not complete browser login
  selector_error — Selectors missing or broken
  scrape_error   — Page loaded but scraping failed unexpectedly
  not_found      — ASIN not found in Fresh catalog or item out of stock
  heal_error     — Heal failed (LLM or DOM issue)
  config_error   — Missing configuration

Usage:
  amazon-fresh past-purchases [--limit N]
  amazon-fresh saved-items [--limit N]
  amazon-fresh search <query> [--limit N]
  amazon-fresh add <asin> [qty]
  amazon-fresh cart
  amazon-fresh remove <asin>
  amazon-fresh edit <asin> <qty>

Global flags:
  --headed        Open a visible browser window
"""

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Resolve package path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent.parent))

logger = logging.getLogger("amazon-fresh")

PAGES = ["past-purchases", "saved-items", "search", "add", "cart"]

PAGE_KEY_MAP = {
    "past-purchases": "past_purchases",
    "saved-items": "saved_items",
    "search": "search",
    "add": "add",
    "cart": "cart",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _err(message: str, code: str) -> None:
    """Print a JSON error to stdout and exit non-zero."""
    print(json.dumps({"error": message, "code": code}, ensure_ascii=False))
    sys.exit(1)


def _out(data: dict) -> None:
    """Print JSON result to stdout."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _open_page(context, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000):
    """Navigate to URL and return page. Exits with auth_expired if redirected to login."""
    from .internal.browser import get_page
    try:
        return get_page(context, url, wait_until=wait_until, timeout=timeout)
    except PermissionError as e:
        _err(str(e), "auth_expired")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_auth(args) -> None:
    """One-time headed login to Amazon Fresh. Saves Firefox profile for future headless runs."""
    from .internal.browser import (
        DEFAULT_PROFILE_DIR,
        launch_browser,
        close_browser,
        is_auth_redirect,
    )

    print(json.dumps({
        "status": "opening_browser",
        "message": "Please log into Amazon Fresh in the browser window. The CLI will detect when you're done.",
        "profile_dir": str(DEFAULT_PROFILE_DIR),
    }), flush=True)

    p, context = launch_browser(headed=True)
    try:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.amazon.com/afx/lists/pastpurchases/fresh",
                  wait_until="domcontentloaded", timeout=60000)

        print(json.dumps({"status": "waiting", "message": "Waiting for you to complete login..."}), flush=True)

        # Poll until we're past the login page (up to 5 minutes)
        for _ in range(60):
            page.wait_for_timeout(5000)
            if not is_auth_redirect(page.url):
                break
        else:
            _err("Login not detected after 5 minutes. Please try again.", "auth_failed")

        _out({
            "status": "success",
            "message": "Amazon Fresh session saved.",
            "profile_dir": str(DEFAULT_PROFILE_DIR),
            "next_steps": [
                "Run 'amazon-fresh heal' to bootstrap selectors from the live pages.",
                f"Then transfer profile to QNAP: scp -r {DEFAULT_PROFILE_DIR}/ admin@<QNAP_IP>:/share/CE_CACHEDEV2_DATA/synapse/credentials/amazon/",
            ],
        })
    finally:
        close_browser(p, context)


def cmd_past_purchases(args) -> None:
    """List past Amazon Fresh purchases using selectors from selectors.json."""
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import get_page_selectors
    from .internal.scraper import scrape_items, scroll_to_load

    try:
        sel = get_page_selectors("past_purchases")
    except KeyError as e:
        _err(str(e), "selector_error")

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, sel["url"])
        scroll_to_load(page, item_selector=sel.get("item_container"), max_items=args.limit or 100)
        items = scrape_items(page, sel, limit=args.limit)
        _out({"items": items, "count": len(items)})
    except Exception as e:
        _err(f"Scrape failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_saved_items(args) -> None:
    """List Amazon Fresh saved items using selectors from selectors.json."""
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import get_page_selectors
    from .internal.scraper import scrape_items, scroll_to_load

    try:
        sel = get_page_selectors("saved_items")
    except KeyError as e:
        _err(str(e), "selector_error")

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, sel["url"])
        if "click_selector" in sel:
            page.locator(sel["click_selector"]).first.click(timeout=10000)
            page.wait_for_timeout(3000)  # Wait for tab to load
        scroll_to_load(page, item_selector=sel.get("item_container"), max_items=args.limit or 100)
        items = scrape_items(page, sel, limit=args.limit)
        _out({"items": items, "count": len(items)})
    except Exception as e:
        _err(f"Scrape failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_search(args) -> None:
    """Search Amazon Fresh. Results with purchase badges sorted first (most recent)."""
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import get_page_selectors
    from .internal.scraper import scrape_search_results

    try:
        sel = get_page_selectors("search")
    except KeyError as e:
        _err(str(e), "selector_error")

    import urllib.parse
    url = sel["url_template"].format(query=urllib.parse.quote_plus(args.query))

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, url)
        results = scrape_search_results(page, sel, limit=args.limit)
        _out({"query": args.query, "results": results, "count": len(results)})
    except Exception as e:
        _err(f"Search failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_add(args) -> None:
    """Add a product to the Amazon Fresh cart by ASIN."""
    import urllib.parse
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import get_page_selectors
    from .internal.scraper import add_to_cart

    try:
        sel = get_page_selectors("add")
    except KeyError as e:
        _err(str(e), "selector_error")

    url = sel["search_url_template"].format(asin=urllib.parse.quote_plus(args.asin))

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, url)
        result = add_to_cart(page, args.asin, qty=args.qty, sel=sel)
        _out(result)
    except ValueError as e:
        _err(str(e), "not_found")
    except RuntimeError as e:
        _err(str(e), "selector_error")
    except Exception as e:
        _err(f"Add failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_cart(args) -> None:
    """View current Amazon Fresh cart contents."""
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import get_page_selectors
    from .internal.scraper import scrape_cart_items

    try:
        sel = get_page_selectors("cart")
    except KeyError as e:
        _err(str(e), "selector_error")

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, sel["url"])
        result = scrape_cart_items(page, sel)
        _out(result)
    except Exception as e:
        _err(f"Cart scrape failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_remove(args) -> None:
    """Remove a product from the Amazon Fresh cart by ASIN."""
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import get_page_selectors
    from .internal.scraper import remove_from_cart

    try:
        sel = get_page_selectors("cart")
    except KeyError as e:
        _err(str(e), "selector_error")

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, sel["url"])
        result = remove_from_cart(page, args.asin, sel)
        _out(result)
    except ValueError as e:
        _err(str(e), "not_found")
    except RuntimeError as e:
        _err(str(e), "selector_error")
    except Exception as e:
        _err(f"Remove failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_edit(args) -> None:
    """Update the quantity of a product in the Amazon Fresh cart by ASIN."""
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import get_page_selectors
    from .internal.scraper import edit_cart_qty

    try:
        sel = get_page_selectors("cart")
    except KeyError as e:
        _err(str(e), "selector_error")

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, sel["url"])
        result = edit_cart_qty(page, args.asin, args.qty, sel)
        _out(result)
    except ValueError as e:
        _err(str(e), "not_found")
    except RuntimeError as e:
        _err(str(e), "selector_error")
    except Exception as e:
        _err(f"Edit failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_sync_history(args) -> None:
    """Sync ASINs from past-purchases into grocery_history.json using an LLM.

    Calls 'amazon-fresh past-purchases' as a subprocess, then asks the LLM
    to map each history entry's generic_name to the best matching ASIN.
    Skips entries that already have an ASIN unless --force.
    """
    import json as _json
    import subprocess as _sp
    from pathlib import Path

    history_file = Path(args.history_file).expanduser().resolve()
    if not history_file.exists():
        _err(f"History file not found: {history_file}", "config_error")

    with open(history_file) as f:
        history = _json.load(f)

    entries = history.get("items", [])
    to_update = [e for e in entries if e.get("asin") is None] if not args.force else entries

    if not to_update:
        _out({
            "status": "nothing_to_update",
            "message": "All entries already have ASINs. Use --force to re-sync.",
        })
        return

    # ── Step 1: fetch past purchases via the existing CLI command ──────────────
    print(_json.dumps({"status": "loading_past_purchases"}), flush=True)

    cmd = [sys.executable, "-m", "services.ingestion.services.amazon_fresh.cli", "past-purchases"]
    if args.headed:
        cmd.append("--headed")

    try:
        result = _sp.run(cmd, capture_output=True, text=True, timeout=120)
        past_purchases_data = _json.loads(result.stdout)
        past_purchases = [p for p in past_purchases_data.get("items", []) if p.get("asin")]
    except Exception as e:
        _err(f"Failed to load past purchases: {e}", "scrape_error")

    if not past_purchases:
        _err("No past purchases with ASINs found — run 'amazon-fresh heal' if selectors are broken.", "scrape_error")

    print(_json.dumps({"status": "past_purchases_loaded", "count": len(past_purchases)}), flush=True)

    # ── Step 2: ask LLM to map generic names → ASINs ──────────────────────────
    categories = [
        {
            "generic_name": e["generic_name"],
            "options": [o["name"] for o in e.get("options", [])],
        }
        for e in to_update
    ]

    prompt = f"""You are mapping grocery category names to Amazon Fresh product ASINs.

You have a list of past purchases (name + ASIN) and a list of grocery categories
(generic_name + option names as hints). For each category, pick the single best
matching ASIN from the past purchases list, or null if nothing fits.

Rules:
- Match semantically — e.g. "bananas" matches "Organic Banana Bunch (4-5 Count)"
- The options list for each category is ordered most-frequently-purchased first —
  prefer matching options[0] over options[1], options[1] over options[2], etc.
- The past purchases list is also ordered most-recently/frequently-purchased first —
  if multiple past purchases match a category equally well, prefer the one that
  appears earlier in the list.
- Each ASIN may only be assigned to ONE category — no duplicates
- If a category has no reasonable match in the past purchases list, use null
- Return ONLY a valid JSON object: keys are generic_name strings, values are ASIN strings or null
- No explanation, no markdown fences

Past purchases (ordered most frequent first):
{_json.dumps([{"name": p["name"], "asin": p["asin"]} for p in past_purchases], indent=2)}

Categories to match (options ordered most frequent first):
{_json.dumps(categories, indent=2)}"""

    try:
        from services.ingestion.providers import get_provider
        provider = get_provider()
        llm_result = provider.generate_response(prompt, auto_retry=False)

        if llm_result.is_error:
            _err(f"LLM error: {llm_result.text[:200]}", "heal_error")

        json_match = re.search(r'\{.*\}', llm_result.text.strip(), re.DOTALL)
        if not json_match:
            _err("LLM did not return valid JSON", "heal_error")

        mapping = _json.loads(json_match.group())
    except Exception as e:
        _err(f"LLM mapping failed: {e}", "heal_error")

    # ── Step 3: write ASINs back to history.json ───────────────────────────────
    entry_index = {e["generic_name"]: e for e in entries}
    updated = []
    no_match = []

    for generic_name, asin in mapping.items():
        entry = entry_index.get(generic_name)
        if not entry:
            continue
        if asin:
            entry["asin"] = asin
            matched_name = next((p["name"] for p in past_purchases if p["asin"] == asin), asin)
            updated.append({"generic_name": generic_name, "asin": asin, "matched_name": matched_name})
        else:
            no_match.append(generic_name)

    with open(history_file, "w") as f:
        _json.dump(history, f, indent=2)

    _out({
        "status": "done",
        "updated": len(updated),
        "no_match": len(no_match),
        "items": updated,
        "not_found_in_past_purchases": no_match,
    })


def cmd_heal(args) -> None:
    """Discover/repair CSS selectors by loading the live page and calling an LLM.

    Always runs headed so the page renders fully (Amazon uses heavy JS).
    """
    from .internal.browser import launch_browser, close_browser
    from .internal.selectors import load_selectors, merge_page_selectors
    from .internal.scraper import dump_dom_for_heal, scroll_to_load

    config = load_selectors()

    # Get search URL template, inject a test query ('bananas') to load a live page for healing
    search_template = config.get("search", {}).get("url_template", "https://www.amazon.com/s?k={query}&i=amazonfresh")
    search_url = search_template.format(query="bananas")

    # Use a real Fresh ASIN (Organic Whole Milk) so the Add to cart button is
    # definitely present in the DOM during heal — a text query like "bananas"
    # might return sponsored/non-Fresh results without the button.
    add_url = config.get("add", {}).get("search_url_template", "").format(asin="B07ZLF9G83")

    url_map = {
        "past-purchases": config.get("past_purchases", {}).get("url"),
        "saved-items": config.get("saved_items", {}).get("url"),
        "search": search_url,
        "add": add_url or search_url,
        "cart": config.get("cart", {}).get("url"),
    }

    # Ensure URLs exist in the config before trying to heal them
    for k, v in url_map.items():
        if not v:
            _err(f"Missing URL config for '{k}' in selectors.json", "config_error")

    pages_to_heal = [args.page] if args.page else PAGES

    results = {}
    p, context = launch_browser(headed=args.headed)
    try:
        for page_name in pages_to_heal:
            page_key = PAGE_KEY_MAP[page_name]
            url = url_map[page_name]
            click_sel = config.get(page_key, {}).get("click_selector")

            print(json.dumps({"status": "healing", "page": page_name, "url": url}), flush=True)

            page = _open_page(context, url, wait_until="networkidle", timeout=45000)
            if click_sel:
                page.locator(click_sel).first.click(timeout=10000)
                page.wait_for_timeout(3000)
            
            page.wait_for_timeout(2000)
            scroll_to_load(page, max_scrolls=3)

            # Dump DOM for LLM analysis.
            # For the add page, scope to a few product cards only — the full
            # search results page is far too large for the LLM and we only
            # need to see the button structure inside one representative card.
            if page_name == "add":
                scope_sel = config.get("add", {}).get("item_container", "[data-component-type='s-search-result']")
                html = dump_dom_for_heal(page, scope_selector=scope_sel, scope_limit=3)
            elif page_name == "cart":
                # Full-page dump for cart: the page is small (~80K) and the
                # subtotal lives outside item containers, so scoping would hide it.
                # Cart must have items for the item selectors to be discoverable.
                html = dump_dom_for_heal(page)
                if page.locator(config.get("cart", {}).get("item_container", "[data-asin]")).count() == 0:
                    print(json.dumps({
                        "status": "warning", "page": "cart",
                        "message": "Cart appears empty — add items before healing the cart page.",
                    }), flush=True)
            else:
                html = dump_dom_for_heal(page)
            print(json.dumps({"status": "dom_captured", "page": page_name, "chars": len(html)}), flush=True)

            # Call LLM to identify selectors
            new_selectors = _llm_identify_selectors(page_name, url, html)
            if not new_selectors:
                results[page_name] = {"status": "failed", "error": "LLM did not return valid selectors"}
                print(json.dumps({"status": "failed", "page": page_name}), flush=True)
                continue

            merge_page_selectors(page_key, new_selectors)
            results[page_name] = {"status": "healed", "selectors": new_selectors}
            print(json.dumps({"status": "healed", "page": page_name, "selectors": new_selectors}), flush=True)

    finally:
        close_browser(p, context)

    _out({"heal_results": results})


def _llm_identify_selectors(page_name: str, url: str, dom_html: str) -> Optional[dict]:
    """Call Claude CLI to identify CSS selectors from DOM HTML.

    This is the single LLM call in the entire CLI. Returns selector dict or None.
    """
    if page_name == "add":
        keys_block = """{
  "item_container": "<selector for each product card — the outermost element per product, e.g. [data-component-type='s-search-result']>",
  "add_to_cart_button": "<selector for the 'Add to cart' button, relative to the item container>",
  "qty_increment_button": "<selector for the '+' quantity increment button that appears inside the card after adding, or null if not visible in this DOM snapshot>",
  "out_of_stock_indicator": "<selector for an out-of-stock badge/text element inside the card, or null>"
}"""
    elif page_name == "cart":
        keys_block = """{
  "item_container": "<selector for each cart item row — the outermost element per item>",
  "item_name": "<selector for product name, relative to the item container>",
  "item_price": "<selector for item price, relative to the item container>",
  "item_qty": "<selector for the quantity field (input or select) inside the item container>",
  "delete_button": "<selector for the delete/remove button, relative to the item container>",
  "qty_input": "<selector for a text input to type a new quantity, relative to the item container, or null>",
  "qty_increment_button": "<selector for the '+' stepper button, relative to the item container, or null>",
  "qty_decrement_button": "<selector for the '-' stepper button, relative to the item container, or null>",
  "subtotal": "<page-level selector (NOT relative to item container) for the cart subtotal/order total amount, or null>"
}"""
    else:
        keys_block = """{
  "item_container": "<selector for each product card — the outermost element per product>",
  "item_name": "<selector for product name, relative to the container>",
  "item_price": "<selector for product price, relative to the container>"
}"""

    prompt = f"""You are analyzing HTML from an Amazon Fresh page to identify stable CSS selectors for product scraping.

Page: {page_name}
URL: {url}

Your task: identify the most stable CSS selectors for scraping product listings.

Rules:
- Prefer selectors using data-* attributes, aria-label, role, or semantic structure over class names.
- Avoid class names that look auto-generated (long hashes, random strings).
- For name/title fields: target the specific child element that contains ONLY the product name text.
  Amazon often renders names inside a container that also holds links and hidden spans — avoid
  selecting the container itself. Look for child spans like .a-truncate-full (the full visible text)
  rather than the parent div/span which would capture all nested text including "Opens in a new tab".
- For quantity fields: note whether the element is an <input>, <select>, or a <button role="combobox">
  — use the most specific selector that identifies the element type.

Return ONLY a valid JSON object with these exact keys (no explanation, no markdown fences):
{keys_block}

If you cannot identify a selector confidently, use null for that key.

HTML:
{dom_html}"""

    try:
        from services.ingestion.providers import get_provider
        provider = get_provider()
        
        # Don't auto-retry with fallback models if it's just a selector task, keep it fast
        result = provider.generate_response(prompt, auto_retry=False)
        
        if result.is_error:
            logger.warning("Provider error: %s", result.text[:200])
            return None

        output = result.text.strip()

        import re
        json_match = re.search(r'\{.*\}', output, re.DOTALL)
        if not json_match:
            logger.warning("No JSON found in LLM response: %s", output[:300])
            return None

        parsed = json.loads(json_match.group())
        # Filter out null values
        filtered = {k: v for k, v in parsed.items() if v is not None}
        if not filtered:
            logger.warning("LLM returned JSON, but all selector values were null. Raw output: %s", output[:500])
        return filtered

    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to parse LLM response: %s", e)
        return None


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def _run_human_command(cmd: str, argv: list) -> None:
    """Parse and dispatch a human-only command (invisible to --help)."""
    p = argparse.ArgumentParser(prog=f"amazon-fresh {cmd}")
    p.add_argument("--headed", action="store_true", default=False, help="Open a visible browser window.")
    if cmd == "auth":
        cmd_auth(p.parse_args(argv))
    elif cmd == "heal":
        p.add_argument("--page", choices=PAGES, default=None, help="Page to heal (default: all).")
        cmd_heal(p.parse_args(argv))
    elif cmd == "sync-history":
        p.add_argument("--history-file", required=True,
                       help="Path to grocery_history.json.")
        p.add_argument("--force", action="store_true", default=False,
                       help="Re-sync entries that already have an ASIN.")
        cmd_sync_history(p.parse_args(argv))


def main():
    logging.basicConfig(level=logging.WARNING)

    # Human-only commands parsed before argparse so they never appear in --help
    _HUMAN_COMMANDS = {"auth", "heal", "sync-history"}
    if len(sys.argv) > 1 and sys.argv[1] in _HUMAN_COMMANDS:
        _run_human_command(sys.argv[1], sys.argv[2:])
        return

    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Open a visible browser window.",
    )

    parser = argparse.ArgumentParser(
        prog="amazon-fresh",
        description="Amazon Fresh CLI for Synapse. All output is JSON to stdout.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pp = sub.add_parser("past-purchases", help="List past Amazon Fresh purchases.", parents=[parent_parser])
    p_pp.add_argument("--limit", type=int, default=None, help="Max items to return.")

    p_si = sub.add_parser("saved-items", help="List Amazon Fresh saved items.", parents=[parent_parser])
    p_si.add_argument("--limit", type=int, default=None, help="Max items to return.")

    p_search = sub.add_parser("search", help="Search Amazon Fresh. Previously-purchased items sorted first.", parents=[parent_parser])
    p_search.add_argument("query", help="Search query.")
    p_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10).")

    p_add = sub.add_parser("add", help="Add a product to the Amazon Fresh cart by ASIN.", parents=[parent_parser])
    p_add.add_argument("asin", help="Product ASIN (from search or past-purchases output).")
    p_add.add_argument("qty", nargs="?", type=int, default=1, help="Quantity to add (default: 1).")

    sub.add_parser("cart", help="View current Amazon Fresh cart contents.", parents=[parent_parser])

    p_remove = sub.add_parser("remove", help="Remove a product from the Amazon Fresh cart by ASIN.", parents=[parent_parser])
    p_remove.add_argument("asin", help="Product ASIN to remove.")

    def _positive_int(value):
        v = int(value)
        if v < 1:
            raise argparse.ArgumentTypeError("qty must be at least 1 — use 'remove' to delete an item")
        return v

    p_edit = sub.add_parser("edit", help="Update quantity of a product in the Amazon Fresh cart.", parents=[parent_parser])
    p_edit.add_argument("asin", help="Product ASIN to update.")
    p_edit.add_argument("qty", type=_positive_int, help="New quantity (must be >= 1; use 'remove' to delete).")

    args = parser.parse_args()

    dispatch = {
        "past-purchases": cmd_past_purchases,
        "saved-items": cmd_saved_items,
        "search": cmd_search,
        "add": cmd_add,
        "cart": cmd_cart,
        "remove": cmd_remove,
        "edit": cmd_edit,
    }

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        _err(f"Unexpected error: {e}", "scrape_error")


if __name__ == "__main__":
    main()
