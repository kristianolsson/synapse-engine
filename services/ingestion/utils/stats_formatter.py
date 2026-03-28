"""
Stats formatting for execution statistics from the Gemini CLI.

Provides channel-specific formatters for email (markdown) and Telegram (compact).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def format_stats_email(stats: Optional[dict]) -> str:
    """
    Format execution stats as an HTML block for email footers.

    Example output:
        <br><br><hr><b>Stats:</b>
        <br>- gemini-2.5-pro: 2 requests, 0 errors, 5053ms
        <br>- google_web_search: 1 call, 1 ok, 0 fail
    """
    if not stats:
        return ""

    try:
        lines = ["<br><br><hr><b>Stats:</b>"]

        # Gemini format: per-model stats
        for name, data in stats.get("models", {}).items():
            api = data.get("api", {})
            reqs = api.get("totalRequests", 0)
            errs = api.get("totalErrors", 0)
            latency = api.get("totalLatencyMs", 0)
            lines.append(f"- {name}: {reqs} request{'s' if reqs != 1 else ''}, {errs} error{'s' if errs != 1 else ''}, {latency}ms")

        # Claude format: per-model usage
        for name, data in stats.get("modelUsage", {}).items():
            tokens_in = data.get("inputTokens", 0)
            tokens_out = data.get("outputTokens", 0)
            cost = data.get("costUSD", 0)
            lines.append(f"- {name}: {tokens_in} in / {tokens_out} out, ${cost:.4f}")

        # Claude: total cost and duration
        if stats.get("total_cost_usd") is not None:
            lines.append(f"- Total cost: ${stats['total_cost_usd']:.4f}")
        if stats.get("duration_ms") is not None:
            lines.append(f"- Duration: {stats['duration_ms']}ms")

        # Claude format: server tools
        server_tools = stats.get("server_tool_use", {})
        if server_tools:
            fetch = server_tools.get("web_fetch_requests", 0)
            search = server_tools.get("web_search_requests", 0)
            if fetch > 0 or search > 0:
                lines.append(f"- Web Tools: {search} search{'es' if search != 1 else ''}, {fetch} fetch{'es' if fetch != 1 else ''}")

        # Gemini format: per-tool stats
        by_name = stats.get("tools", {}).get("byName", {})
        for tool_name, tool_data in by_name.items():
            count = tool_data.get("count", 0)
            ok = tool_data.get("success", 0)
            fail = tool_data.get("fail", 0)
            lines.append(f"- {tool_name}: {count} call{'s' if count != 1 else ''}, {ok} ok, {fail} fail")

        return "<br>".join(lines)
    except Exception as e:
        logger.warning("Failed to format stats for email: %s", e)
        return ""


def format_stats_telegram(stats: Optional[dict]) -> str:
    """
    Format execution stats as a compact one-liner for Telegram messages.

    Example output:
        [Stats: gemini-2.5-pro (2 req, 0 err) | google_web_search: 1 (1 ok)]
    """
    if not stats:
        return ""

    try:
        parts = []

        # Gemini format: per-model stats
        for name, data in stats.get("models", {}).items():
            api = data.get("api", {})
            reqs = api.get("totalRequests", 0)
            errs = api.get("totalErrors", 0)
            parts.append(f"{name} ({reqs} req, {errs} err)")

        # Claude format: per-model usage
        for name, data in stats.get("modelUsage", {}).items():
            tokens_in = data.get("inputTokens", 0)
            tokens_out = data.get("outputTokens", 0)
            cost = data.get("costUSD", 0)
            parts.append(f"{name} ({tokens_in}in/{tokens_out}out, ${cost:.4f})")

        # Claude format: server tools
        server_tools = stats.get("server_tool_use", {})
        if server_tools:
            fetch = server_tools.get("web_fetch_requests", 0)
            search = server_tools.get("web_search_requests", 0)
            if fetch > 0 or search > 0:
                parts.append(f"web: {search}s/{fetch}f")

        # Gemini format: per-tool stats
        by_name = stats.get("tools", {}).get("byName", {})
        for tool_name, tool_data in by_name.items():
            count = tool_data.get("count", 0)
            ok = tool_data.get("success", 0)
            fail = tool_data.get("fail", 0)
            if fail > 0:
                parts.append(f"{tool_name}: {count} ({ok} ok, {fail} fail)")
            else:
                parts.append(f"{tool_name}: {count} ({ok} ok)")

        if not parts:
            return ""

        return f"\n\n[Stats: {' | '.join(parts)}]"
    except Exception as e:
        logger.warning("Failed to format stats for Telegram: %s", e)
        return ""
