"""Tests for the stats formatting module."""

from services.ingestion.stats_formatter import format_stats_email, format_stats_telegram


class TestFormatStatsEmail:
    def test_none_returns_empty(self):
        assert format_stats_email(None) == ""

    def test_empty_dict_returns_empty(self):
        assert format_stats_email({}) == ""

    def test_single_model(self):
        stats = {
            "models": {
                "gemini-2.5-pro": {
                    "api": {"totalRequests": 2, "totalErrors": 0, "totalLatencyMs": 5000}
                }
            }
        }
        result = format_stats_email(stats)
        assert "gemini-2.5-pro: 2 requests, 0 errors, 5000ms" in result
        assert result.startswith("\n\n---")

    def test_multi_model(self):
        stats = {
            "models": {
                "gemini-2.5-pro": {
                    "api": {"totalRequests": 2, "totalErrors": 0, "totalLatencyMs": 5000}
                },
                "gemini-2.5-flash": {
                    "api": {"totalRequests": 1, "totalErrors": 1, "totalLatencyMs": 2000}
                },
            }
        }
        result = format_stats_email(stats)
        assert "gemini-2.5-pro: 2 requests, 0 errors, 5000ms" in result
        assert "gemini-2.5-flash: 1 request, 1 error, 2000ms" in result

    def test_with_tools(self):
        stats = {
            "models": {
                "gemini-pro": {
                    "api": {"totalRequests": 1, "totalErrors": 0, "totalLatencyMs": 100}
                }
            },
            "tools": {
                "byName": {
                    "google_web_search": {"count": 2, "success": 2, "fail": 0},
                    "read_file": {"count": 1, "success": 0, "fail": 1},
                }
            },
        }
        result = format_stats_email(stats)
        assert "google_web_search: 2 calls, 2 ok, 0 fail" in result
        assert "read_file: 1 call, 0 ok, 1 fail" in result

    def test_no_tools_section(self):
        stats = {
            "models": {
                "gemini-pro": {
                    "api": {"totalRequests": 1, "totalErrors": 0, "totalLatencyMs": 100}
                }
            }
        }
        result = format_stats_email(stats)
        assert "gemini-pro" in result
        # No tool lines
        assert "call" not in result.split("gemini-pro")[1] or "request" in result


class TestFormatStatsTelegram:
    def test_none_returns_empty(self):
        assert format_stats_telegram(None) == ""

    def test_empty_dict_returns_empty(self):
        assert format_stats_telegram({}) == ""

    def test_single_model(self):
        stats = {
            "models": {
                "gemini-2.5-pro": {
                    "api": {"totalRequests": 2, "totalErrors": 0}
                }
            }
        }
        result = format_stats_telegram(stats)
        assert "[Stats: gemini-2.5-pro (2 req, 0 err)]" in result

    def test_multi_model(self):
        stats = {
            "models": {
                "gemini-2.5-pro": {
                    "api": {"totalRequests": 2, "totalErrors": 0}
                },
                "gemini-2.5-flash": {
                    "api": {"totalRequests": 1, "totalErrors": 1}
                },
            }
        }
        result = format_stats_telegram(stats)
        assert "gemini-2.5-pro (2 req, 0 err)" in result
        assert "gemini-2.5-flash (1 req, 1 err)" in result

    def test_with_tools_no_fail(self):
        stats = {
            "models": {
                "gemini-pro": {"api": {"totalRequests": 1, "totalErrors": 0}}
            },
            "tools": {
                "byName": {
                    "google_web_search": {"count": 1, "success": 1, "fail": 0}
                }
            },
        }
        result = format_stats_telegram(stats)
        assert "google_web_search: 1 (1 ok)" in result

    def test_with_tools_with_fail(self):
        stats = {
            "models": {
                "gemini-pro": {"api": {"totalRequests": 1, "totalErrors": 0}}
            },
            "tools": {
                "byName": {
                    "google_web_search": {"count": 2, "success": 1, "fail": 1}
                }
            },
        }
        result = format_stats_telegram(stats)
        assert "google_web_search: 2 (1 ok, 1 fail)" in result
