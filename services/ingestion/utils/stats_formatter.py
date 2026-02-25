"""
Stats formatting for execution statistics from the Gemini CLI.

Provides channel-specific formatters for email (markdown) and Telegram (compact).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def format_stats_email(stats: Optional[dict]) -> str:
    """
    Format execution stats as a markdown block for email footers.

    Example output:
        ---
        **Stats:**
        - gemini-2.5-pro: 2 requests, 0 errors, 5053ms
        - google_web_search: 1 call, 1 ok, 0 fail
    """
    if not stats:
        return ""

    try:
        lines = ["\n\n---\n**Stats:**"]

        # Per-model stats
        for name, data in stats.get("models", {}).items():
            api = data.get("api", {})
            reqs = api.get("totalRequests", 0)
            errs = api.get("totalErrors", 0)
            latency = api.get("totalLatencyMs", 0)
            lines.append(f"- {name}: {reqs} request{'s' if reqs != 1 else ''}, {errs} error{'s' if errs != 1 else ''}, {latency}ms")

        # Per-tool stats
        by_name = stats.get("tools", {}).get("byName", {})
        for tool_name, tool_data in by_name.items():
            count = tool_data.get("count", 0)
            ok = tool_data.get("success", 0)
            fail = tool_data.get("fail", 0)
            lines.append(f"- {tool_name}: {count} call{'s' if count != 1 else ''}, {ok} ok, {fail} fail")

        return "\n".join(lines) + "\n"
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

        # Per-model stats
        for name, data in stats.get("models", {}).items():
            api = data.get("api", {})
            reqs = api.get("totalRequests", 0)
            errs = api.get("totalErrors", 0)
            parts.append(f"{name} ({reqs} req, {errs} err)")

        # Per-tool stats
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
