#!/usr/bin/env python3
"""Options Bot CLI — runs the weekday options opportunity scan.

Fetches E*TRADE data, analyzes options opportunities, and outputs an HTML report
with <!-- TICKER_CONTEXT:SYMBOL --> merge fields. The calling LLM session populates
these fields with per-ticker research from the stock-researcher sub-agent.

Output is HTML to stdout. The synapse scheduler delivers it via email.

On error, outputs {"error": "<message>", "code": "<error_type>"} to stdout and exits non-zero.

Usage:
  options-bot scan --tickers AAPL,MSFT,GOOGL
  options-bot scan --tickers AAPL
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Resolve package path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

logger = logging.getLogger("options-bot")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _err(message: str, code: str) -> None:
    """Print a JSON error to stdout and exit non-zero."""
    print(json.dumps({"error": message, "code": code}, ensure_ascii=False))
    sys.exit(1)


def _load_env() -> dict:
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


def _load_options_config(config_path: Optional[str] = None) -> dict:
    """Load options_config.yaml from the vault."""
    from services.ingestion import config as syn_config
    path = (
        config_path
        or os.getenv("STOCKS_OPTIONS_CONFIG_PATH")
        or str(Path(syn_config.VAULT_PATH) / "stocks" / "options_config.yaml")
    )
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        _err(f"Failed to load options config from {path}: {e}", "config_error")


def _authenticate(env: dict):
    from services.ingestion.tools.stocks.auth import ETradeAuth
    from services.ingestion.tools.stocks.wetrade_auth import WetradeAuth, WETRADE_AVAILABLE
    from services.ingestion.tools.stocks.etrade_client import ETradeClient

    if not env["consumer_key"] or not env["consumer_secret"]:
        _err("ETRADE_CONSUMER_KEY and ETRADE_CONSUMER_SECRET must be set in .env", "config_error")

    sandbox = env["mode"].lower() == "sandbox"
    use_wetrade = WETRADE_AVAILABLE and env["username"] and env["password"]

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
        access_token, access_token_secret = auth.authenticate(headless=True)
    except Exception as e:
        _err(f"E*TRADE authentication failed: {e}", "auth_failed")

    return ETradeClient(
        consumer_key=env["consumer_key"],
        consumer_secret=env["consumer_secret"],
        access_token=access_token,
        access_token_secret=access_token_secret,
        sandbox=sandbox,
    )


# ── HTML Builder ──────────────────────────────────────────────────────────────

def _format_currency(val) -> str:
    if val is None:
        return "N/A"
    return f"${val:,.2f}"


def _format_pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.2f}%"


def _build_markdown(
    tickers: list[str],
    by_ticker: dict,
    buying_power: Optional[dict],
    position_recommendations: list,
    ticker_quotes: dict,
) -> str:
    """Build a text-friendly Markdown report for Telegram/CLI viewing."""
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## 🎯 Options Trading Opportunities\n_{scan_time}_\n"]

    # Account status section
    if buying_power:
        margin_bp = buying_power.get("margin_buying_power", 0) or 0
        cash_bp = buying_power.get("cash_buying_power", 0) or 0
        account_val = buying_power.get("account_value", 0) or 0
        lines.append("### Account Status")
        lines.append(f"- **Margin Buying Power:** {_format_currency(margin_bp)}")
        lines.append(f"- **Cash Buying Power:** {_format_currency(cash_bp)}")
        lines.append(f"- **Account Value:** {_format_currency(account_val)}\n")

    # Open positions section
    if position_recommendations:
        lines.append("### 📊 Open Position Recommendations")
        for rec in position_recommendations:
            pnl_pct = rec.profit_loss_pct
            pnl_str = f"{pnl_pct:+.1%}"
            lines.append(f"- **{rec.action_emoji} {rec.action}** {rec.position.underlying} ${rec.position.strike_price:.0f}P exp {rec.position.expiration_date} (DTE: {rec.position.days_to_expiry}d) | P/L: {pnl_str} | _{rec.reason}_")
        lines.append("")

    lines.append("### 💡 Portfolio Analysis & Action Plan")
    lines.append("<!-- PORTFOLIO_ANALYSIS -->\n")

    all_opps = [opp for opps in by_ticker.values() for opp in opps]
    tickers_with_opps = sorted(by_ticker.keys(), key=lambda t: max(o.score for o in by_ticker[t]), reverse=True)
    other_tickers = sorted([t for t in tickers if t not in by_ticker])
    sorted_tickers = tickers_with_opps + other_tickers

    lines.append(f"Found **{len(all_opps)}** opportunities across **{len(tickers_with_opps)}** tickers.\n")

    for ticker in sorted_tickers:
        opps = by_ticker.get(ticker, [])
        quote_data = ticker_quotes.get(ticker, {})
        current_price = quote_data.get("current_price") or (opps[0].underlying_price if opps else 0)
        price_change_pct = quote_data.get("price_change_pct") or (opps[0].price_change_pct if opps else None)

        direction_str = ""
        if price_change_pct is not None:
            direction_str = f"▲ {_format_pct(abs(price_change_pct))}" if price_change_pct >= 0 else f"▼ {_format_pct(abs(price_change_pct))}"

        lines.append(f"### {ticker} @ {_format_currency(current_price)} {direction_str}")
        lines.append(f"<!-- TICKER_CONTEXT:{ticker} -->\n")

        if opps:
            for i, opp in enumerate(opps):
                rank_badge = "🥇" if i == 0 else ("🥈" if i == 1 else ("🥉" if i == 2 else "  "))
                lines.append(f"- {rank_badge} {opp.score:.0f} | **${opp.contract.strike_price:.2f}** | {opp.contract.expiration_date} ({opp.contract.days_to_expiry}d) | Bid: {_format_currency(opp.contract.bid)} | Yld: {_format_pct(opp.annualized_yield)} | Prot: {_format_pct(opp.downside_protection)}")
            lines.append("")
        else:
            lines.append("_No opportunities met your thresholds._\n")

    return "\n".join(lines)


def _build_html(
    tickers: list[str],
    by_ticker: dict,
    buying_power: Optional[dict],
    position_recommendations: list,
    ticker_quotes: dict,
) -> str:
    """Build the full HTML report with TICKER_CONTEXT merge fields."""

    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Account status section
    bp_html = ""
    if buying_power:
        margin_bp = buying_power.get("margin_buying_power", 0) or 0
        cash_bp = buying_power.get("cash_buying_power", 0) or 0
        account_val = buying_power.get("account_value", 0) or 0
        bp_html = f"""
        <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
            <h4 style="margin: 0 0 10px 0; color: #333;">Account Status</h4>
            <table style="width: 100%;">
                <tr>
                    <td style="padding: 4px 0;"><strong>Margin Buying Power:</strong></td>
                    <td style="padding: 4px 0; text-align: right; color: #28a745; font-weight: bold;">{_format_currency(margin_bp)}</td>
                </tr>
                <tr>
                    <td style="padding: 4px 0;"><strong>Cash Buying Power:</strong></td>
                    <td style="padding: 4px 0; text-align: right;">{_format_currency(cash_bp)}</td>
                </tr>
                <tr>
                    <td style="padding: 4px 0;"><strong>Account Value:</strong></td>
                    <td style="padding: 4px 0; text-align: right;">{_format_currency(account_val)}</td>
                </tr>
            </table>
        </div>
        """

    # Open positions section
    pos_html = ""
    if position_recommendations:
        rows = []
        for rec in position_recommendations:
            pnl_pct = rec.profit_loss_pct
            pnl_color = "#28a745" if pnl_pct >= 0 else "#dc3545"
            pnl_str = f"{pnl_pct:+.1%}"
            action_colors = {
                "CLOSE": ("#dc3545", "#fff"),
                "HOLD": ("#28a745", "#fff"),
                "ROLL": ("#ffc107", "#000"),
            }
            bg_color, text_color = action_colors.get(rec.action, ("#6c757d", "#fff"))
            rows.append(f"""
            <tr>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">
                    <span style="background: {bg_color}; color: {text_color}; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 12px;">
                        {rec.action_emoji} {rec.action}
                    </span>
                </td>
                <td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">{rec.position.underlying}</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">${rec.position.strike_price:.0f}P</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{rec.position.expiration_date}</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{rec.position.days_to_expiry}d</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee; color: {pnl_color}; font-weight: bold;">{pnl_str}</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee; font-size: 12px; color: #666;">{rec.reason}</td>
            </tr>""")
        pos_html = f"""
        <div style="margin-bottom: 20px;">
            <h3 style="color: #333; border-bottom: 2px solid #6c757d; padding-bottom: 5px;">📊 Open Position Recommendations</h3>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <thead>
                    <tr style="background: #f8f9fa;">
                        <th style="padding: 8px; text-align: left;">Action</th>
                        <th style="padding: 8px; text-align: left;">Ticker</th>
                        <th style="padding: 8px; text-align: left;">Strike</th>
                        <th style="padding: 8px; text-align: left;">Expiry</th>
                        <th style="padding: 8px; text-align: left;">DTE</th>
                        <th style="padding: 8px; text-align: left;">P/L</th>
                        <th style="padding: 8px; text-align: left;">Reason</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(rows)}
                </tbody>
            </table>
        </div>"""

    # Ticker sections
    all_opps = [opp for opps in by_ticker.values() for opp in opps]
    tickers_with_opps = sorted(by_ticker.keys(), key=lambda t: max(o.score for o in by_ticker[t]), reverse=True)
    other_tickers = sorted([t for t in tickers if t not in by_ticker])
    sorted_tickers = tickers_with_opps + other_tickers

    ticker_sections = []
    for ticker in sorted_tickers:
        opps = by_ticker.get(ticker, [])
        quote_data = ticker_quotes.get(ticker, {})
        current_price = quote_data.get("current_price") or (opps[0].underlying_price if opps else 0)
        price_change_pct = quote_data.get("price_change_pct") or (opps[0].price_change_pct if opps else None)

        if price_change_pct is not None:
            if price_change_pct >= 0:
                direction_html = f'<span style="color: #28a745;">▲ {_format_pct(abs(price_change_pct))}</span>'
            else:
                direction_html = f'<span style="color: #dc3545;">▼ {_format_pct(abs(price_change_pct))}</span>'
        else:
            direction_html = ""

        # TICKER_CONTEXT merge field — LLM replaces this with stock-researcher output
        merge_field = f"<!-- TICKER_CONTEXT:{ticker} -->"

        if opps:
            rows = []
            for i, opp in enumerate(opps):
                delta_str = f"{abs(opp.contract.delta):.2f}" if opp.contract.delta else "N/A"
                rank_badge = "🥇" if i == 0 else ("🥈" if i == 1 else ("🥉" if i == 2 else ""))
                rows.append(f"""
                <tr>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; font-weight: bold;">{rank_badge} {opp.score:.0f}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">${opp.contract.strike_price:.2f}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{opp.contract.expiration_date}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{opp.contract.days_to_expiry}d</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{_format_currency(opp.contract.bid)}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee; color: #28a745; font-weight: bold;">{_format_pct(opp.annualized_yield)}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{_format_pct(opp.downside_protection)}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{delta_str}</td>
                    <td style="padding: 8px; border-bottom: 1px solid #eee;">{opp.contract.open_interest}</td>
                </tr>""")
            opps_html = f"""
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <thead>
                    <tr style="background: #f8f9fa;">
                        <th style="padding: 8px; text-align: left;">Score</th>
                        <th style="padding: 8px; text-align: left;">Strike</th>
                        <th style="padding: 8px; text-align: left;">Expiry</th>
                        <th style="padding: 8px; text-align: left;">DTE</th>
                        <th style="padding: 8px; text-align: left;">Bid</th>
                        <th style="padding: 8px; text-align: left;">Yield (Ann.)</th>
                        <th style="padding: 8px; text-align: left;">Protection</th>
                        <th style="padding: 8px; text-align: left;">Delta</th>
                        <th style="padding: 8px; text-align: left;">OI</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(rows)}
                </tbody>
            </table>"""
        else:
            opps_html = '<p style="color: #888; font-style: italic; margin-bottom: 20px;">No opportunities met your thresholds.</p>'

        ticker_sections.append(f"""
        <h3 style="color: #333; border-bottom: 2px solid #007bff; padding-bottom: 5px;">
            {ticker} @ {_format_currency(current_price)} {direction_html}
        </h3>
        {merge_field}
        {opps_html}""")

    return f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #333;">🎯 Options Trading Opportunities</h2>
    <p style="color: #666;">{scan_time}</p>

    {bp_html}

    {pos_html}

    <div style="margin-bottom: 24px; padding: 14px 16px; background: #f5f0ff; border-left: 4px solid #6f42c1; border-radius: 4px;">
        <h4 style="margin: 0 0 8px 0; color: #4a235a;">💡 Portfolio Analysis &amp; Action Plan</h4>
        <!-- PORTFOLIO_ANALYSIS -->
    </div>

    <p>Found <strong>{len(all_opps)}</strong> opportunities across <strong>{len(tickers_with_opps)}</strong> tickers.</p>

    {"".join(ticker_sections)}

    <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
    <p style="color: #999; font-size: 12px;">
        Automated options scan via Synapse.<br>
        All trading decisions are your responsibility.
    </p>
</body>
</html>"""


# ── Scan command ──────────────────────────────────────────────────────────────

def cmd_scan(args) -> None:
    """Run the weekday options scan for a given list of tickers."""
    from services.ingestion.tools.stocks.etrade_client import ETradeClient
    from services.ingestion.tools.stocks.analyzer import OptionsAnalyzer
    from services.ingestion.tools.stocks.position_analyzer import PositionAnalyzer

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        _err("No tickers provided. Use --tickers AAPL,MSFT,GOOGL", "config_error")

    env = _load_env()
    cfg = _load_options_config(getattr(args, "config", None))
    thresholds = cfg.get("thresholds", {})
    strategy = cfg.get("strategy", "cash_secured_puts")
    option_type = "PUT" if strategy == "cash_secured_puts" else "CALL"
    account_suffix = cfg.get("accounts", {}).get("default")

    target_days = thresholds.get("target_days_to_expiry", 45)
    min_days = thresholds.get("min_days_to_expiry", 30)
    max_days = thresholds.get("max_days_to_expiry", 60)

    try:
        client = _authenticate(env)
        if account_suffix:
            client.account_suffix = account_suffix
    except SystemExit:
        raise
    except Exception as e:
        _err(f"Authentication error: {e}", "auth_failed")

    # Fetch buying power
    buying_power = None
    try:
        buying_power = client.get_buying_power()
    except Exception as e:
        logger.warning("Could not fetch buying power: %s", e)

    # Fetch open positions and generate recommendations
    position_recommendations = []
    try:
        positions = client.get_short_put_positions()
        if positions:
            exit_thresholds = cfg.get("exit_thresholds", {})
            analyzer = PositionAnalyzer(**exit_thresholds)
            position_recommendations = analyzer.analyze_positions(positions)
    except Exception as e:
        logger.warning("Could not fetch positions: %s", e)

    # Scan each ticker for opportunities
    by_ticker = {}
    ticker_quotes = {}
    options_analyzer = OptionsAnalyzer(thresholds)

    for ticker in tickers:
        try:
            quote_raw = client.get_quote(ticker)
            all_data = quote_raw.get("All", {})
            current_price = all_data.get("lastTrade") or all_data.get("ask") or 0
            prev_close = all_data.get("previousClose")
            price_change_pct = None
            if prev_close and current_price:
                price_change_pct = (current_price - prev_close) / prev_close

            ticker_quotes[ticker] = {
                "current_price": current_price,
                "price_change_pct": price_change_pct,
                "next_earning_date": all_data.get("nextEarningDate"),
            }

            exp_date, contracts = client.get_options_for_ticker(
                symbol=ticker,
                target_days=target_days,
                min_days=min_days,
                max_days=max_days,
                option_type=option_type,
            )

            if contracts:
                opportunities = options_analyzer.analyze_chain(
                    contracts, current_price, filter_passing_only=True
                )
                top = options_analyzer.get_top_opportunities(opportunities, top_n=5)
                if top:
                    by_ticker[ticker] = top

        except Exception as e:
            logger.warning("Error scanning %s: %s", ticker, e)
            ticker_quotes[ticker] = {"current_price": None, "price_change_pct": None}

    if getattr(args, "format", "html") == "markdown":
        output = _build_markdown(
            tickers=tickers,
            by_ticker=by_ticker,
            buying_power=buying_power,
            position_recommendations=position_recommendations,
            ticker_quotes=ticker_quotes,
        )
    else:
        output = _build_html(
            tickers=tickers,
            by_ticker=by_ticker,
            buying_power=buying_power,
            position_recommendations=position_recommendations,
            ticker_quotes=ticker_quotes,
        )
    print(output)


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(
        prog="options-bot",
        description="Options Bot CLI for Synapse — weekday options opportunity scan.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Run the options opportunity scan")
    p_scan.add_argument(
        "--tickers",
        required=True,
        metavar="TICKER[,TICKER,...]",
        help="Comma-separated list of tickers to scan (e.g. AAPL,MSFT,GOOGL)",
    )
    p_scan.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to options_config.yaml (default: from vault)",
    )
    p_scan.add_argument(
        "--format",
        choices=["html", "markdown"],
        default="html",
        help="Output format: html or markdown (default: html)",
    )

    args = parser.parse_args()

    try:
        if args.command == "scan":
            cmd_scan(args)
    except SystemExit:
        raise
    except Exception as e:
        _err(f"Unexpected error: {e}", "api_error")


if __name__ == "__main__":
    main()
