#!/usr/bin/env python3
"""E*TRADE CLI — query E*TRADE for quotes, options, positions, and account data.

This CLI is a reusable tool for the Synapse ecosystem. All output is JSON to stdout.
On error, outputs {"error": "<message>", "code": "<error_type>"} and exits non-zero.

Error codes:
  auth_failed   — E*TRADE authentication failed
  api_error     — E*TRADE API returned an unexpected response
  timeout       — API call timed out
  config_error  — Missing or invalid configuration

Usage:
  etrade quote AAPL MSFT GOOGL
  etrade options AAPL [--days 45]
  etrade positions [--account SUFFIX]
  etrade balance [--account SUFFIX]
  etrade accounts
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Resolve stocks package path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from services.ingestion.tools.stocks.auth import ETradeAuth
from services.ingestion.tools.stocks.wetrade_auth import WetradeAuth, WETRADE_AVAILABLE
from services.ingestion.tools.stocks.etrade_client import ETradeClient
from services.ingestion.tools.stocks.analyzer import OptionsAnalyzer

logger = logging.getLogger("etrade-cli")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _err(message: str, code: str) -> None:
    """Print a JSON error to stdout and exit non-zero."""
    print(json.dumps({"error": message, "code": code}, ensure_ascii=False))
    sys.exit(1)


def _load_env() -> dict:
    """Load E*TRADE credentials from environment."""
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    load_dotenv(env_path)

    return {
        "consumer_key": os.getenv("ETRADE_CONSUMER_KEY", ""),
        "consumer_secret": os.getenv("ETRADE_CONSUMER_SECRET", ""),
        "username": os.getenv("ETRADE_USERNAME", ""),
        "password": os.getenv("ETRADE_PASSWORD", ""),
        "totp_secret": os.getenv("ETRADE_TOTP_SECRET", ""),
        "mode": os.getenv("ETRADE_MODE", "production"),
    }


def _authenticate(env: dict, headless: bool = True) -> ETradeClient:
    """Authenticate with E*TRADE and return a client. Calls _err() on failure."""
    if not env["consumer_key"] or not env["consumer_secret"]:
        _err("ETRADE_CONSUMER_KEY and ETRADE_CONSUMER_SECRET must be set in .env", "config_error")

    sandbox = env["mode"].lower() == "sandbox"
    use_wetrade = (
        WETRADE_AVAILABLE
        and env["username"]
        and env["password"]
    )

    try:
        if use_wetrade:
            auth = WetradeAuth(
                consumer_key=env["consumer_key"],
                consumer_secret=env["consumer_secret"],
                username=env["username"],
                password=env["password"],
                sandbox=sandbox,
                totp_secret=env.get("totp_secret") or None,
            )
        else:
            auth = ETradeAuth(
                consumer_key=env["consumer_key"],
                consumer_secret=env["consumer_secret"],
                sandbox=sandbox,
            )
        access_token, access_token_secret = auth.authenticate(headless=headless)
    except Exception as e:
        _err(f"E*TRADE authentication failed: {e}", "auth_failed")

    return ETradeClient(
        consumer_key=env["consumer_key"],
        consumer_secret=env["consumer_secret"],
        access_token=access_token,
        access_token_secret=access_token_secret,
        sandbox=sandbox,
    )


def _get_account_suffix(args_account: Optional[str]) -> Optional[str]:
    """Resolve account suffix: CLI arg > env var > None (uses first account)."""
    if args_account:
        return args_account
    return os.getenv("ETRADE_ACCOUNT_SUFFIX", "") or None


# ── Subcommands ───────────────────────────────────────────────────────────────

def cmd_quote(args, env: dict) -> None:
    """Fetch real-time quotes for one or more tickers."""
    client = _authenticate(env)
    results = {}

    for ticker in args.tickers:
        try:
            raw = client.get_quote(ticker)
            all_data = raw.get("All", {})
            last = all_data.get("lastTrade") or all_data.get("ask") or 0
            prev_close = all_data.get("previousClose")
            change_pct = None
            if prev_close and last:
                change_pct = round((last - prev_close) / prev_close * 100, 4)

            results[ticker] = {
                "ticker": ticker,
                "last_price": last,
                "previous_close": prev_close,
                "change_pct": change_pct,
                "volume": all_data.get("totalVolume"),
                "next_earnings_date": all_data.get("nextEarningDate") or None,
            }
        except Exception as e:
            results[ticker] = {"ticker": ticker, "error": str(e), "code": "api_error"}

    print(json.dumps(results, ensure_ascii=False, default=str))


def cmd_options(args, env: dict) -> None:
    """Fetch options chain and analyze opportunities for a single ticker."""
    from services.ingestion import config as syn_config

    # Load options config from vault
    options_config_path = (
        args.config
        or os.getenv("STOCKS_OPTIONS_CONFIG_PATH")
        or str(Path(syn_config.VAULT_PATH) / "stocks" / "options_config.yaml")
    )
    try:
        import yaml
        with open(options_config_path) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        _err(f"Failed to load options config from {options_config_path}: {e}", "config_error")

    thresholds = cfg.get("thresholds", {})
    strategy = cfg.get("strategy", "cash_secured_puts")
    option_type = "PUT" if strategy == "cash_secured_puts" else "CALL"

    target_days = args.days or thresholds.get("target_days_to_expiry", 45)
    min_days = thresholds.get("min_days_to_expiry", 30)
    max_days = thresholds.get("max_days_to_expiry", 60)

    client = _authenticate(env)
    analyzer = OptionsAnalyzer(thresholds)

    ticker = args.ticker.upper()
    try:
        quote_raw = client.get_quote(ticker)
        all_data = quote_raw.get("All", {})
        current_price = all_data.get("lastTrade") or all_data.get("ask") or 0

        exp_date, contracts = client.get_options_for_ticker(
            symbol=ticker,
            target_days=target_days,
            min_days=min_days,
            max_days=max_days,
            option_type=option_type,
        )

        if not contracts:
            print(json.dumps({"ticker": ticker, "opportunities": [], "message": "No options data found"}, ensure_ascii=False))
            return

        opportunities = analyzer.analyze_chain(contracts, current_price, filter_passing_only=True)
        top = analyzer.get_top_opportunities(opportunities, top_n=5)

        output = {
            "ticker": ticker,
            "current_price": current_price,
            "expiration_date": str(exp_date) if exp_date else None,
            "strategy": strategy,
            "opportunities": [
                {
                    "score": round(opp.score, 2),
                    "strike_price": opp.contract.strike_price,
                    "expiration_date": str(opp.contract.expiration_date),
                    "days_to_expiry": opp.contract.days_to_expiry,
                    "bid": opp.contract.bid,
                    "annualized_yield": round(opp.annualized_yield, 4),
                    "downside_protection": round(opp.downside_protection, 4),
                    "delta": opp.contract.delta,
                    "open_interest": opp.contract.open_interest,
                }
                for opp in top
            ],
        }
        print(json.dumps(output, ensure_ascii=False, default=str))
    except Exception as e:
        _err(f"Failed to fetch options for {ticker}: {e}", "api_error")


def cmd_positions(args, env: dict) -> None:
    """List open short put positions."""
    suffix = _get_account_suffix(getattr(args, "account", None))
    client = _authenticate(env)
    client.account_suffix = suffix

    try:
        positions = client.get_short_put_positions()
        output = [
            {
                "underlying": p.underlying,
                "option_type": p.option_type,
                "strike_price": p.strike_price,
                "expiration_date": str(p.expiration_date),
                "days_to_expiry": p.days_to_expiry,
                "quantity": p.quantity,
                "current_price": p.current_price,
                "cost_basis_per_share": p.cost_basis_per_share,
                "profit_loss_pct": round(p.profit_loss_pct, 4),
                "profit_loss_dollars": round(p.profit_loss, 2),
            }
            for p in positions
        ]
        print(json.dumps(output, ensure_ascii=False, default=str))
    except Exception as e:
        _err(f"Failed to fetch positions: {e}", "api_error")


def cmd_balance(args, env: dict) -> None:
    """Get account buying power and balance."""
    suffix = _get_account_suffix(getattr(args, "account", None))
    client = _authenticate(env)
    client.account_suffix = suffix

    try:
        bp = client.get_buying_power()
        if not bp:
            _err("No balance data returned from E*TRADE", "api_error")
        print(json.dumps(bp, ensure_ascii=False, default=str))
    except Exception as e:
        _err(f"Failed to fetch balance: {e}", "api_error")


def cmd_accounts(args, env: dict) -> None:
    """List all E*TRADE accounts."""
    client = _authenticate(env)

    try:
        data = client.accounts.list_accounts()
        account_list = (
            data.get("AccountListResponse", {})
            .get("Accounts", {})
            .get("Account", [])
        )
        if isinstance(account_list, dict):
            account_list = [account_list]

        output = [
            {
                "account_id": a.get("accountId"),
                "account_id_key": a.get("accountIdKey"),
                "account_type": a.get("accountType"),
                "account_desc": a.get("accountDesc"),
                "account_mode": a.get("accountMode"),
                "status": a.get("accountStatus"),
                "institution_type": a.get("institutionType"),
            }
            for a in account_list
        ]
        print(json.dumps(output, ensure_ascii=False, default=str))
    except Exception as e:
        _err(f"Failed to fetch accounts: {e}", "api_error")


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.WARNING)  # Suppress INFO noise; errors go to stderr

    parser = argparse.ArgumentParser(
        prog="etrade",
        description="E*TRADE CLI for Synapse — query quotes, options, positions, and accounts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # quote
    p_quote = sub.add_parser("quote", help="Get real-time quotes for one or more tickers")
    p_quote.add_argument("tickers", nargs="+", metavar="TICKER", help="Ticker symbol(s)")

    # options
    p_opts = sub.add_parser("options", help="Fetch options chain and analyze opportunities")
    p_opts.add_argument("ticker", metavar="TICKER", help="Ticker symbol")
    p_opts.add_argument("--days", type=int, default=None, help="Target days to expiry (default: from config)")
    p_opts.add_argument("--config", type=str, default=None, help="Path to options_config.yaml")

    # positions
    p_pos = sub.add_parser("positions", help="List open short put positions")
    p_pos.add_argument("--account", type=str, default=None, help="Account suffix (e.g. 2057)")

    # balance
    p_bal = sub.add_parser("balance", help="Get account buying power and balance")
    p_bal.add_argument("--account", type=str, default=None, help="Account suffix (e.g. 2057)")

    # accounts
    sub.add_parser("accounts", help="List all E*TRADE accounts")

    args = parser.parse_args()
    env = _load_env()

    dispatch = {
        "quote": cmd_quote,
        "options": cmd_options,
        "positions": cmd_positions,
        "balance": cmd_balance,
        "accounts": cmd_accounts,
    }

    try:
        dispatch[args.command](args, env)
    except SystemExit:
        raise
    except Exception as e:
        _err(f"Unexpected error: {e}", "api_error")


if __name__ == "__main__":
    main()
