# Reminders Module

This module governs reminder creation and scheduling using the `reminder` command-line tool.

To interact with reminders, you MUST run the `reminder` tool, which is globally available in your environment.
Run `reminder --help` and `reminder <command> --help` to discover all available commands and arguments. Never invoke the tool without running `--help` first, even if you believe you know the subcommand or flag names.

## Intent Extraction

When an incoming message contains reminder intent (e.g., "remind me...", "every morning...", "snooze"), classify it as a **Reminder** in addition to any primary classification (e.g., a todo + reminder). Process the primary route normally, then create the reminder using `reminder add`.

- **Confirmation:** When a reminder is successfully created, include the details in the response: the reminder text, when it will fire, and the delivery channel. Example: "Reminder set: Call the dentist — tomorrow (2026-03-09) at 07:00 via Telegram."

## Reminder Types

- **message** — A simple notification that will be sent directly to the user at the scheduled time. No AI processing at delivery time. Use for straightforward reminders (e.g., "Buy milk", "Call the dentist").
- **work** — A task requiring research, vault queries, or other agentic work before delivering the result. The AI will execute the task at the scheduled time and send the output. Use when the reminder needs live data or analysis (e.g., "Check if there's an F1 race this weekend", "Research stock market sentiment").

## Rules

- **Direct file edits forbidden:** Never modify `reminders.json` directly. All reminder operations (create, edit, delete) must go through the `reminder` CLI. Direct edits bypass validation, auto-commit hooks, and ID generation.
- **One-shot vs Recurring:** Extract user intent to determine if the request is one-shot or recurring. If unclear from context, ask for clarification before creating the reminder.
- **Explicit Times:** All times passed to the CLI must be explicit. Convert relative times (e.g., "3pm", "tomorrow morning") to the required format: full ISO datetime for one-shot (`2026-06-22T07:00:00`), `HH:MM` for recurring (`07:00`, `15:30`).
- **Timezone:** All times are in America/Los_Angeles (Pacific Time). No timezone flag is needed.
- **Errors:** If the CLI returns an error, notify the user and do not proceed.

## Time Defaults

When the user does not specify an exact time, apply these defaults:
- **hourly** → 00:00
- **daily** / **morning** → 07:00
- **evening** → 19:00
- **end of day** → 16:00
- **weekly** → Sunday 07:00
- **snooze** → Next Saturday 07:00

## Channel Defaults

- **One-shot reminders** → `telegram` by default
- **Recurring reminders** → `email` by default
- The user may override the channel explicitly (e.g., "email me a reminder to...").
