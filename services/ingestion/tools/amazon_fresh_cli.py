#!/usr/bin/env python3
"""Amazon Fresh CLI — browse and manage Amazon Fresh via Playwright Firefox.

This CLI is a reusable tool for the Synapse ecosystem. All output is JSON to stdout.
On error, outputs {"error": "<message>", "code": "<error_type>"} and exits non-zero.

Normal operation uses zero LLM calls — CSS selectors are read from selectors.json.
The 'heal' command is the only operation that calls an LLM: it dumps the live page
DOM and asks the LLM to discover/repair selectors, writing them back to selectors.json.

Error codes:
  auth_expired   — Amazon session expired; run 'amazon-fresh auth' on your Mac
  auth_failed    — Could not complete browser login
  selector_error — Selectors missing or broken; run 'amazon-fresh heal'
  scrape_error   — Page loaded but scraping failed unexpectedly
  heal_error     — Heal failed (LLM or DOM issue)
  config_error   — Missing configuration

Usage:
  amazon-fresh auth
  amazon-fresh past-purchases [--limit N]
  amazon-fresh saved-items [--limit N]
  amazon-fresh search <query> [--limit N]
  amazon-fresh heal [--page {past-purchases,saved-items,search}]

Global flags:
  --headed        Open a visible browser window (always on for auth and heal)
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Resolve package path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

logger = logging.getLogger("amazon-fresh")

PAGES = ["past-purchases", "saved-items", "search"]

PAGE_KEY_MAP = {
    "past-purchases": "past_purchases",
    "saved-items": "saved_items",
    "search": "search",
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
    from services.ingestion.tools.amazon_fresh.browser import get_page
    try:
        return get_page(context, url, wait_until=wait_until, timeout=timeout)
    except PermissionError as e:
        _err(str(e), "auth_expired")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_auth(args) -> None:
    """One-time headed login to Amazon Fresh. Saves Firefox profile for future headless runs."""
    from services.ingestion.tools.amazon_fresh.browser import (
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
    from services.ingestion.tools.amazon_fresh.browser import launch_browser, close_browser
    from services.ingestion.tools.amazon_fresh.selectors import get_page_selectors
    from services.ingestion.tools.amazon_fresh.scraper import scrape_items, scroll_to_load

    try:
        sel = get_page_selectors("past_purchases")
    except KeyError as e:
        _err(str(e), "selector_error")

    p, context = launch_browser(headed=args.headed)
    try:
        page = _open_page(context, sel["url"])
        scroll_to_load(page)
        items = scrape_items(page, sel, limit=args.limit)
        _out({"items": items, "count": len(items)})
    except Exception as e:
        _err(f"Scrape failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_saved_items(args) -> None:
    """List Amazon Fresh saved items using selectors from selectors.json."""
    from services.ingestion.tools.amazon_fresh.browser import launch_browser, close_browser
    from services.ingestion.tools.amazon_fresh.selectors import get_page_selectors
    from services.ingestion.tools.amazon_fresh.scraper import scrape_items, scroll_to_load

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
        scroll_to_load(page)
        items = scrape_items(page, sel, limit=args.limit)
        _out({"items": items, "count": len(items)})
    except Exception as e:
        _err(f"Scrape failed: {e}", "scrape_error")
    finally:
        close_browser(p, context)


def cmd_search(args) -> None:
    """Search Amazon Fresh. Results with purchase badges sorted first (most recent)."""
    from services.ingestion.tools.amazon_fresh.browser import launch_browser, close_browser
    from services.ingestion.tools.amazon_fresh.selectors import get_page_selectors
    from services.ingestion.tools.amazon_fresh.scraper import scrape_search_results

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


def cmd_heal(args) -> None:
    """Discover/repair CSS selectors by loading the live page and calling an LLM.

    This is the ONLY command that calls an LLM. Normal scraping uses selectors.json directly.
    Always runs headed so the page renders fully (Amazon uses heavy JS).
    """
    from services.ingestion.tools.amazon_fresh.browser import launch_browser, close_browser
    from services.ingestion.tools.amazon_fresh.selectors import load_selectors, merge_page_selectors
    from services.ingestion.tools.amazon_fresh.scraper import dump_dom_for_heal, scroll_to_load

    config = load_selectors()

    # Get search URL template, inject a test query ('bananas') to load a live page for healing
    search_template = config.get("search", {}).get("url_template", "https://www.amazon.com/s?k={query}&i=amazonfresh")
    search_url = search_template.format(query="bananas")

    url_map = {
        "past-purchases": config.get("past_purchases", {}).get("url"),
        "saved-items": config.get("saved_items", {}).get("url"),
        "search": search_url,
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

            # Dump DOM for LLM analysis
            dom_html = dump_dom_for_heal(page)
            print(json.dumps({"status": "dom_captured", "page": page_name, "chars": len(dom_html)}), flush=True)

            # Call LLM to identify selectors
            new_selectors = _llm_identify_selectors(page_name, url, dom_html)
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
    prompt = f"""You are analyzing HTML from an Amazon Fresh page to identify stable CSS selectors for product scraping.

Page: {page_name}
URL: {url}

Your task: identify the most stable CSS selectors for scraping product listings.
Strongly prefer selectors using data-asin, data-component-type, aria-label, or semantic structure.
Avoid brittle class names that look like they could be auto-generated (e.g. long hashes).

Return ONLY a valid JSON object with these exact keys (no explanation, no markdown fences):
{{
  "item_container": "<selector for each product card — the outermost element per product>",
  "item_name": "<selector for product name, relative to the container>",
  "item_price": "<selector for product price, relative to the container>"
}}

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

    except subprocess.TimeoutExpired:
        logger.warning("Claude CLI timed out during heal")
        return None
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to parse LLM response: %s", e)
        return None


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(
        prog="amazon-fresh",
        description=(
            "Amazon Fresh CLI for Synapse. Uses CSS selectors from selectors.json "
            "(zero LLM calls in normal operation). Run 'heal' first to bootstrap selectors."
        ),
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Open a visible browser window (auto-enabled for auth and heal).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="One-time headed login — saves Firefox session for headless use.")

    p_pp = sub.add_parser("past-purchases", help="List past Amazon Fresh purchases.")
    p_pp.add_argument("--limit", type=int, default=None, help="Max items to return.")

    p_si = sub.add_parser("saved-items", help="List Amazon Fresh saved items.")
    p_si.add_argument("--limit", type=int, default=None, help="Max items to return.")

    p_search = sub.add_parser("search", help="Search Amazon Fresh. Previously-purchased items sorted first.")
    p_search.add_argument("query", help="Search query.")
    p_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10).")

    p_heal = sub.add_parser(
        "heal",
        help=(
            "Bootstrap or repair selectors.json by loading live pages and calling an LLM. "
            "Run this first after 'auth', or whenever scraping breaks."
        ),
    )
    p_heal.add_argument("--page", choices=PAGES, default=None, help="Page to heal (default: all).")

    args = parser.parse_args()

    dispatch = {
        "auth": cmd_auth,
        "past-purchases": cmd_past_purchases,
        "saved-items": cmd_saved_items,
        "search": cmd_search,
        "heal": cmd_heal,
    }

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        _err(f"Unexpected error: {e}", "scrape_error")


if __name__ == "__main__":
    main()
