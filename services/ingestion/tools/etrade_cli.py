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


def _send_pin_auth_prompt(pending: dict) -> bool:
    """Notify the user that E*TRADE needs manual re-authentication, via
    whichever channel is configured, and record which one so the reply
    can be matched back to this prompt. Returns True if a prompt was
    actually delivered."""
    from services.ingestion import config
    from services.ingestion.tools.stocks import etrade_pin_auth

    prompt_text = (
        "E*TRADE needs manual re-authentication — automated login is blocked "
        "by their fraud detection.\n\n"
        f"Open this link on your phone and log in normally:\n{pending['authorize_url']}\n\n"
        "E*TRADE will show a verification code on screen — reply to this "
        "message with that code to finish."
    )

    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_USER_IDS:
        from services.ingestion.channels.telegram.sender import send_telegram_message
        chat_id = config.TELEGRAM_ALLOWED_USER_IDS[0]
        message_id = send_telegram_message(chat_id, prompt_text)
        if message_id:
            etrade_pin_auth.mark_prompt_sent(channel="telegram", chat_id=chat_id, prompt_message_id=message_id)
            return True

    if config.REPLY_TO_ADDRESS:
        import email.utils
        from services.ingestion.channels.email.reply import send_reply
        subject = "Synapse: E*TRADE re-authentication needed"
        prompt_message_id = email.utils.make_msgid()
        if send_reply(
            to_addr=config.REPLY_TO_ADDRESS, subject=subject, body=prompt_text, message_id=prompt_message_id
        ):
            etrade_pin_auth.mark_prompt_sent(channel="email", prompt_message_id=prompt_message_id)
            return True

    return False


def _fallback_to_pin_auth(env: dict, original_error: Exception) -> None:
    """Automated login failed with no one there to complete it — send a
    manual PIN-auth prompt via Telegram/email and exit. The listener that
    receives the reply completes the exchange (see etrade_pin_auth.py and
    the /update-etrade-auth wiring). Always exits via _err()."""
    from services.ingestion.tools.stocks import etrade_pin_auth

    logger.warning("Automated E*TRADE login failed (%s) — sending manual re-auth prompt", original_error)

    if etrade_pin_auth.load_pending():
        _err(
            "E*TRADE authentication failed and a re-auth prompt is already pending — "
            "reply to it with the verification code.",
            "auth_pending",
        )

    try:
        pending = etrade_pin_auth.start_pin_auth(env["consumer_key"], env["consumer_secret"])
    except Exception as e:
        _err(f"E*TRADE authentication failed ({original_error}); could not start PIN fallback: {e}", "auth_failed")

    # Capture retry correlation, if the caller supplied any (set by the
    # channel listeners / scheduler via pipe_to_provider's extra_env).
    # Presence of session_key or reminder_task is what marks this pending
    # request as retryable — a manual /update-etrade-auth run never sets
    # these, so it's correctly excluded from any retry attempt.
    correlation = {}
    session_key = os.environ.get("SYNAPSE_SESSION_KEY", "")
    reminder_task = os.environ.get("SYNAPSE_REMINDER_TASK", "")
    if session_key:
        correlation["session_key"] = session_key
        correlation["session_id"] = os.environ.get("SYNAPSE_SESSION_ID", "")
        correlation["failed_command"] = " ".join(sys.argv)
    elif reminder_task:
        correlation["reminder_task"] = reminder_task
    if correlation:
        retry_channel = os.environ.get("SYNAPSE_CHANNEL", "")
        if retry_channel:
            correlation["retry_channel"] = retry_channel
        if os.environ.get("SYNAPSE_CHAT_ID"):
            correlation["chat_id"] = int(os.environ["SYNAPSE_CHAT_ID"])
        if os.environ.get("SYNAPSE_EMAIL_TO"):
            correlation["email_to"] = os.environ["SYNAPSE_EMAIL_TO"]
            correlation["email_subject"] = os.environ.get("SYNAPSE_EMAIL_SUBJECT", "")
            correlation["email_message_id"] = os.environ.get("SYNAPSE_EMAIL_MESSAGE_ID", "")
            correlation["email_references"] = os.environ.get("SYNAPSE_EMAIL_REFERENCES", "")
        etrade_pin_auth.mark_prompt_sent(**correlation)

    if not _send_pin_auth_prompt(pending):
        etrade_pin_auth.clear_pending()
        _err(
            f"E*TRADE authentication failed ({original_error}); no Telegram/email channel "
            "configured to send a manual re-auth prompt.",
            "auth_failed",
        )

    _err(
        "E*TRADE authentication failed — sent a manual re-authentication prompt, waiting on a reply.",
        "auth_pending",
    )


def _authenticate(env: dict, headless: bool = True) -> ETradeClient:
    """Authenticate with E*TRADE and return a client. Calls _err() on failure.

    When unattended (headless=True, the default — no one present to
    complete a stuck login), a failed automated attempt falls back to a
    manual OAuth PIN prompt via Telegram/email instead of erroring out
    silently. --headed runs (a human already watching) just report the
    failure directly.
    """
    if not env["consumer_key"] or not env["consumer_secret"]:
        _err("ETRADE_CONSUMER_KEY and ETRADE_CONSUMER_SECRET must be set in .env", "config_error")

    sandbox = env["mode"].lower() == "sandbox"
    use_wetrade = (
        WETRADE_AVAILABLE
        and env["username"]
        and env["password"]
    )
    # An unattended caller has no one to complete a stuck login — fail
    # fast instead of waiting the full manual-SMS window before falling
    # back to the PIN-code flow, and don't retry a login we've confirmed
    # is deterministically blocked (retrying just doubles the wait).
    login_timeout_ms = 120000 if not headless else 20000
    max_retries = 2 if not headless else 1

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
            access_token, access_token_secret = auth.authenticate(
                headless=headless, login_timeout_ms=login_timeout_ms, max_retries=max_retries
            )
        else:
            auth = ETradeAuth(
                consumer_key=env["consumer_key"],
                consumer_secret=env["consumer_secret"],
                sandbox=sandbox,
            )
            access_token, access_token_secret = auth.authenticate(headless=headless)
    except Exception as e:
        if headless:
            _fallback_to_pin_auth(env, e)
        else:
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
    client = _authenticate(env, headless=not args.headed)
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

    client = _authenticate(env, headless=not args.headed)
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
    """List all open positions."""
    suffix = _get_account_suffix(getattr(args, "account", None))
    client = _authenticate(env, headless=not args.headed)
    client.account_suffix = suffix

    try:
        positions = client.get_all_positions()
        print(json.dumps(positions, ensure_ascii=False, default=str))
    except Exception as e:
        _err(f"Failed to fetch positions: {e}", "api_error")


def cmd_balance(args, env: dict) -> None:
    """Get account buying power and balance."""
    suffix = _get_account_suffix(getattr(args, "account", None))
    client = _authenticate(env, headless=not args.headed)
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
    client = _authenticate(env, headless=not args.headed)

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

# Lock file path — serializes all etrade CLI invocations to prevent race
# conditions when parallel calls try to authenticate/refresh the same token.
LOCK_FILE = "/tmp/etrade.lock"


def main():
    import fcntl

    logging.basicConfig(level=logging.WARNING)  # Suppress INFO noise; errors go to stderr

    parser = argparse.ArgumentParser(
        prog="etrade",
        description="E*TRADE CLI for Synapse — query quotes, options, positions, and accounts.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser for manual login/SMS entry instead of running headless "
             "(use this for the one-time bootstrap auth on a machine with a display)",
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

    # Acquire exclusive file lock to serialize all etrade CLI calls.
    # The `with` block guarantees the lock is released on success, exception,
    # or crash (OS releases flock when the file descriptor is closed).
    with open(LOCK_FILE, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            dispatch[args.command](args, env)
        except SystemExit:
            raise
        except Exception as e:
            _err(f"Unexpected error: {e}", "api_error")


if __name__ == "__main__":
    main()
