# Calendar Module

This module governs calendar operations using the `calendar` command-line tool.

To interact with the calendar, you MUST run the `calendar` tool, which is globally available in your environment.
Run `calendar --help` and `calendar <command> --help` to discover all available commands and arguments. Never invoke the tool without running `--help` first, even if you believe you know the subcommand or flag names.

## Rules
- **Global Search by Default:** Unless the user explicitly asks for a single specific calendar (e.g., "show me Marias schedule"), you MUST leave the `--calendar` parameter empty when listing events. This ensures you fetch events from ALL configured calendars, which is critical to avoid missing family events, children's sports, or regional holidays (e.g., Swedish holidays).
- **Deduplication:** The CLI may return duplicate events if they appear on multiple calendars. Merge these logically before presenting to the user.
- **Output formatting:** The CLI returns raw text. Parse it and present it cleanly.
- **All-day events:** When creating all-day events using `--start` and `--end` (e.g., "vacation March 15-17"), use the date-only format `YYYY-MM-DD`. Remember that Google Calendar uses an **exclusive** end date for all-day events, so "March 15-17" means `--start="2026-03-15" --end="2026-03-18"`.
- **External Party Confirmation:** Calendar changes involving external parties (teachers, coaches, etc.) MUST be confirmed with the user before executing. Present the proposed change and ask for explicit approval first.
- **Errors:** If the CLI returns an error, notify the user and do not proceed.
- **Event dates:** Never infer an event's date from its ID. Recurring event IDs contain UTC timestamps (e.g., `_20260531T000000Z`) that may not match the local date. The section header (e.g., `## Saturday, May 30, 2026`) is authoritative for the event's local date.