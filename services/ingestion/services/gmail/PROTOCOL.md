# Email Module

This module governs Gmail operations using the `gmail` command-line tool.

To interact with Gmail, you MUST run the `gmail` tool, which is globally available in your environment.
Run `gmail --help` and `gmail <command> --help` to discover all available commands and arguments.

## Rules
- **Drafts only:** You can compose and reply to emails, but only as drafts. Never attempt to send — there is no send command.
- **Label resolution:** `apply-label` and `remove-label` accept label names, not IDs. Use `list-labels` first if you are unsure of the exact name.
- **Errors:** If the CLI returns an error, notify the user and do not proceed.
