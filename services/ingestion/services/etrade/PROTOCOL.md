# Stocks Module

This module governs stock market operations using the `etrade` and `options-bot`
CLI tools, and the `stock-researcher` sub-agent.

## Tools

Tools and agents that are globally available in your environment:
- `etrade` — Tool to query E*TRADE for quotes, options chains, positions, and account balance.
- `options-bot` — Tool to run the options opportunity scan for a given set of tickers.
- `stock-researcher` — Sub-agent for focused web research on a single ticker. Delegate one instance per ticker for parallel research.

You MUST run `<tool> --help` and `<tool> <subcommand> --help` to discover all available commands and arguments before invoking `etrade` or `options-bot`. Never guess subcommands or flags.

## Configuration

Stock configuration is split across files in the `stocks/` directory:
- `options_watchlist.md`: tickers for the options bot.
- `market_watchlist.md`: tickers for market monitoring.
- `options_config.yaml`: options strategy settings and thresholds. Do NOT modify without explicit user confirmation.

## Working with Options Bot Output

Opportunity scores are composite 0–100: yield (0–30 pts), downside protection (0–25 pts), delta safety (0–25 pts, lower delta scores higher), liquidity (0–15 pts), spread tightness (0–5 pts). 70+ is solid, 85+ is strong.

When `options-bot scan` returns output (HTML or Markdown), replace all merge fields before returning.

**`<!-- TICKER_CONTEXT:SYMBOL -->`** — for each opportunity returned by options-bot scan delegate to `stock-researcher` with this prompt. Only delegate for tickers that have actual opportunity rows in their table. If options-bot outputs "No opportunities met your thresholds" for a ticker, skip the stock-researcher call entirely — the merge field will not be present in the HTML for those tickers.
> "Research [TICKER] for the options scan: (1) one-sentence summary of the most important recent catalyst or news, (2) current analyst sentiment (bullish/neutral/bearish with brief reason), (3) next earnings date. Be concise — output is used in a single paragraph. Today is [DATE]."

Replace the merge field with the result:
```html
<p style="color: #555; font-size: 13px; font-style: italic; margin: 5px 0 10px;">
  📰 [stock-researcher result]. Next earnings: [date if available].
</p>
```

**`<!-- PORTFOLIO_ANALYSIS -->`** — replace with 1-2 short paragraphs of judgment only. Do NOT do arithmetic in prose — every number needed (P/L, Fwd Yld, collateral, affordability ✓/✗) is already computed in the tables; reference values, never recompute them. Cover: (1) whether each open position is worth keeping given market context and the switch rule below, (2) which new opportunities are most compelling and why.
```html
<p style="color: #333; font-size: 14px; margin: 6px 0;">[paragraph]</p>
```

**Switch rule.** The bot's HOLD/CLOSE/ROLL action reflects exit thresholds only (profit target, DTE, max loss) — it does NOT consider opportunity cost, so you must. Compare each new opportunity's annualized yield against each open position's `Fwd Yld (Ann.)` column: if the new yield beats the forward yield by at least the point threshold stated in the switch-rule note under the positions table AND the new opportunity's score is 70+, recommend CLOSE & SWITCH even when the bot says HOLD. Otherwise prefer holding — closing early costs commissions and slippage.

**Affordability.** Buying Power in Account Status is what is available to trade. Each opportunity's Collateral column already shows ✓ (affordable) or ✗ (not affordable) — trust it, don't recompute. An opportunity marked ✗ is still actionable via CLOSE & SWITCH if an open position's freed Collateral plus Buying Power covers it.

**`<!-- ACTION_PLAN -->`** — replace with a single HTML table and nothing else (no surrounding prose). One row per decision; every open position and every ticker with opportunities appears exactly once. Order rows: DO first, then WATCH, then SKIP. "Why" is one clause, max ~12 words.
```html
<table style="width: 100%; border-collapse: collapse;">
    <thead>
        <tr style="background: rgba(0,0,0,0.05);">
            <th style="padding: 6px 8px; text-align: left;">Action</th>
            <th style="padding: 6px 8px; text-align: left;">Trade</th>
            <th style="padding: 6px 8px; text-align: left;">Why</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td style="padding: 6px 8px; border-bottom: 1px solid #ddd; white-space: nowrap;"><strong>[✅ DO | 👀 WATCH | ⏭️ SKIP | 🔄 CLOSE &amp; SWITCH | 🟢 HOLD]</strong></td>
            <td style="padding: 6px 8px; border-bottom: 1px solid #ddd; white-space: nowrap;">[e.g. Sell NVDA $185P 8/21]</td>
            <td style="padding: 6px 8px; border-bottom: 1px solid #ddd;">[one clause]</td>
        </tr>
    </tbody>
</table>
```

Return the complete merged HTML — the scheduler delivers it as an email.

## Market Summary

Produces a cohesive HTML email covering all tickers in `market_watchlist.md`, rendered
from the fixed template `stocks/market_summary_template.html`. Never invent your own
layout, CSS, or sections — read the template and fill its placeholders exactly as its
embedded comments describe. Every summary must look identical in structure week to week;
only the data and prose change.

**Data authority:**
- `etrade quote` is authoritative for price and 52W range on `equity` and `etf` tickers.
- `stock-researcher` is authoritative for 1-week price moves and all data on `crypto` tickers — skip `etrade` for crypto entirely.

**Workflow:**
1. Read `market_watchlist.md` for tickers and their `asset_type`, and read `stocks/market_summary_template.html`.
2. Fetch `etrade quote` for all non-crypto tickers.
3. Run all research in parallel:
   - **Macro:** delegate one `stock-researcher` instance with this prompt:
     > "Gather current macro market conditions: (1) current VIX level AND the VIX level approximately one week ago (both as numbers), (2) S&P 500 YTD performance, (3) 10-year Treasury yield and whether rates are rising/falling/stable, (4) next FOMC meeting date and what the market expects (hold, cut, or hike), (5) any other major macro events this week (jobs report, CPI, FOMC, etc.) with dates. Focus on what these mean for an individual stock investor, not just raw numbers. Today is [DATE]."
   - **Per ticker:** delegate one `stock-researcher` instance per ticker with this prompt:
     > "Research [TICKER] ([asset_type]): (1) current price and 1-week % move, (2) the most important recent catalyst or news in one sentence, (3) analyst sentiment (bullish/neutral/bearish with brief reason), (4) next earnings date or key upcoming event. Today is [DATE]."
     
     Expected output — return exactly these labeled fields:
     ```
     Price: $X
     1W Move: +X.X%
     Sentiment: Bullish/Neutral/Bearish — [reason]
     Catalyst: [one sentence]
     Upcoming: [event and date, or "none"]
     ```

4. Fill the template placeholders:

   **VIX interpretation (fixed bands — never a judgment call):**

   | VIX | Emoji + label |
   |---|---|
   | < 15 | 😎 Calm |
   | 15–20 | 🙂 Normal |
   | 20–30 | 😐 Elevated |
   | 30+ | 😰 Stressed |

   - The label comes ONLY from this table — the same VIX number must always get the same label.
   - Trend arrow vs. one week ago: ↑ if up ≥ 1 point, ↓ if down ≥ 1 point, → otherwise. Render as e.g. `🙂 Normal (VIX 18.2 ↑, was 14.1)`. If last week's VIX cannot be found, omit the arrow and "was" clause rather than guessing.
   - The FIRST sentence of the mood context must be a dedicated plain-English VIX read: what the level and direction mean for day-to-day swings and hedging-cost signal. Example: "Volatility is in the normal range and drifting up from calm — day-to-day swings are unremarkable, but hedging demand is picking up."
   - Follow it with ONE soft action-framing sentence tied to the band — framed as conditions, never as an instruction to buy or sell a specific position, and never derived from VIX alone as a mechanical trigger:
     - 😎 Calm → "Options premium is cheap — a favorable window for buying protection; selling premium earns less."
     - 🙂 Normal → "No volatility edge either way — position sizing and entries matter more than timing volatility."
     - 😐 Elevated → "Options premium is rich — conditions favor selling premium (e.g., cash-secured puts) over chasing entries, with wider strikes for safety."
     - 😰 Stressed → "Crisis pricing — avoid mechanical moves in either direction; premium is extreme for a reason."
     Adapt the wording to the week's context; the framing (not the exact sentence) is what's fixed.

   **Alert block** — include only when a binary macro event (CPI, FOMC, jobs report) lands in the upcoming week; otherwise omit entirely.

   **Ticker rows** — one row per watchlist ticker in `market_watchlist.md` order, using exactly the row shape in the template's `TICKER_ROWS` comment. Sentiment badges: 🟢 Bullish / 🟡 Neutral / 🔴 Bearish. Move classes: `move-pos` / `move-neg` / `move-flat` (use `~flat` for moves within ±0.5%).

   **Week-ahead outlook** — 3-4 paragraphs per the template's `OUTLOOK` comment.

5. Output the completed HTML document and nothing else: it must start with `<!DOCTYPE html>` and end with `</html>` — no markdown code fences, no commentary, no stats. All template placeholder brackets and instruction comments must be gone from the final output.

Return the complete HTML as your response — output is sent directly as an email body.

## User Requests

For ad-hoc stock queries:
- Stock quote → `etrade quote <TICKER>` and format the result for the user
- Options scan → `options-bot scan --tickers <TICKER> --format markdown` for a single ticker
- Portfolio → `etrade positions` and `etrade balance`
- Accounts → `etrade accounts`
- Watchlist changes → Update `stocks/options_watchlist.md` or `stocks/market_watchlist.md`
  - If the user says "add X to my watchlist" without specifying which, ask:
    "Which watchlist — options (for trading) or market (for monitoring)?"
  - Always confirm the change after writing it
- Strategy/threshold changes → confirm before modifying `stocks/options_config.yaml`

## Rules

- Use `etrade` CLI for real-time E*TRADE data (prices, options, positions).
- Use `stock-researcher` sub-agent for web research (news, sentiment, catalysts).
- When a CLI tool returns pre-formatted HTML, include it verbatim in your response.
  Do not strip or simplify HTML output from tools — the delivery system handles it.
