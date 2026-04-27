#!/usr/bin/env python3
"""
Reminder CLI tool for synapse-engine.

Provides subcommands to add, edit, remove, and list reminders stored in
reminders.json. Used by AI providers to manage reminders programmatically.

Usage:
    reminder add --type work --task "..." --time "15:00" --day friday --recurring weekly --channel telegram
    reminder edit --id UUID [--time "16:00"] [--task "new text"] [--channel email]
    reminder remove --id UUID
    reminder list [--json]
"""

import argparse
import fcntl
import json
import os
import sys
import uuid
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# Default path — overridden by REMINDERS_JSON_PATH env var or --config flag
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_REMINDERS_PATH = _ROOT / "notes" / "reminders" / "reminders.json"

VALID_RECURRING = {"none", "daily", "weekly", "weekdays", "monthly"}
VALID_TYPES = {"message", "work"}
VALID_CHANNELS = {"telegram", "email"}
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def _resolve_reminders_path(args_config: Optional[Path] = None) -> Path:
    """Resolve the reminders.json path from args, env, or default."""
    if args_config:
        return args_config
    env_path = os.getenv("REMINDERS_JSON_PATH", "")
    if env_path:
        return Path(env_path)
    return DEFAULT_REMINDERS_PATH


def _lock_path(path: Path) -> Path:
    """Return the lockfile path for a given reminders.json path."""
    return path.with_suffix(".json.lock")


def _read_reminders(path: Path) -> list[dict]:
    """Read reminders from JSON file with file locking."""
    if not path.exists():
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error reading {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, list):
        print(f"Error: {path} must contain a JSON array.", file=sys.stderr)
        sys.exit(1)

    return data


def _write_reminders(path: Path, data: list[dict]) -> None:
    """Write reminders to JSON file atomically with file locking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, path)
    except IOError as e:
        print(f"Error writing {path}: {e}", file=sys.stderr)
        if tmp_path.exists():
            tmp_path.unlink()
        sys.exit(1)


def _locked_read_modify_write(path: Path, modify_fn) -> tuple[list[dict], any]:
    """Read-modify-write with exclusive lockfile to prevent concurrent overwrites.

    Args:
        path: Path to reminders.json
        modify_fn: Callable that takes the current list and returns (new_list, result).

    Returns:
        The result value from modify_fn.
    """
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = _read_reminders(path)
            new_data, result = modify_fn(data)
            _write_reminders(path, new_data)
            return result
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _validate_time_format(time_str: str, recurring: str) -> None:
    """Validate time format based on recurring type.

    Raises:
        ValueError: If the time format is invalid for the given recurring type.
    """
    if recurring == "none":
        # Must be full ISO datetime
        try:
            datetime.fromisoformat(time_str)
        except ValueError:
            raise ValueError(
                f"For one-shot reminders (recurring=none), --time must be a full ISO datetime "
                f"(e.g., 2026-06-22T07:00:00). Got: '{time_str}'"
            )
    else:
        # Must be HH:MM
        parts = time_str.split(":")
        if len(parts) != 2:
            raise ValueError(
                f"For recurring reminders, --time must be HH:MM (e.g., 07:00). Got: '{time_str}'"
            )
        try:
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError()
        except ValueError:
            raise ValueError(
                f"For recurring reminders, --time must be HH:MM with valid hour (0-23) and minute (0-59). Got: '{time_str}'"
            )


def _validate_day(day: str, recurring: str) -> None:
    """Validate the day field based on recurring type.

    Raises:
        ValueError: If the day is invalid for the given recurring type.
    """
    if recurring == "weekly":
        if day.lower() not in WEEKDAYS:
            raise ValueError(
                f"For weekly reminders, --day must be a weekday name "
                f"(monday, tuesday, ..., sunday). Got: '{day}'"
            )
    elif recurring == "monthly":
        if day.lower() == "last":
            return
        try:
            day_num = int(day)
            if not (1 <= day_num <= 31):
                raise ValueError()
        except ValueError:
            raise ValueError(
                f"For monthly reminders, --day must be a day number (1-31) or 'last'. Got: '{day}'"
            )


def cmd_add(args, reminders_path: Path) -> str:
    """Add a new reminder."""
    # Validate before acquiring lock
    if args.type not in VALID_TYPES:
        raise ValueError(f"--type must be one of: {', '.join(sorted(VALID_TYPES))}. Got: '{args.type}'")
    if args.channel not in VALID_CHANNELS:
        raise ValueError(f"--channel must be one of: {', '.join(sorted(VALID_CHANNELS))}. Got: '{args.channel}'")
    if args.recurring not in VALID_RECURRING:
        raise ValueError(f"--recurring must be one of: {', '.join(sorted(VALID_RECURRING))}. Got: '{args.recurring}'")

    _validate_time_format(args.time, args.recurring)

    if args.recurring in ("weekly", "monthly"):
        if not args.day:
            raise ValueError(f"--day is required for {args.recurring} reminders.")
        _validate_day(args.day, args.recurring)
    elif args.recurring in ("daily", "weekdays", "none"):
        if args.day:
            raise ValueError(f"--day is not applicable for {args.recurring} reminders.")

    reminder = {
        "id": str(uuid.uuid4()).upper(),
        "type": args.type,
        "channel": args.channel,
        "task": args.task,
        "time": args.time,
        "recurring": args.recurring,
        "created_at": datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if args.day:
        reminder["day"] = args.day.lower()
    if args.source:
        reminder["source"] = args.source

    def _do_add(data):
        data.append(reminder)
        return data, None

    _locked_read_modify_write(reminders_path, _do_add)

    out = StringIO()
    out.write(f"Reminder created: {reminder['id']}\n")
    out.write(f"  Type: {reminder['type']}\n")
    out.write(f"  Channel: {reminder['channel']}\n")
    out.write(f"  Task: {reminder['task']}\n")
    out.write(f"  Time: {reminder['time']}")
    if args.day:
        out.write(f" ({reminder['day']})")
    out.write(f"\n  Recurring: {reminder['recurring']}\n")

    return out.getvalue()


def cmd_edit(args, reminders_path: Path) -> str:
    """Edit an existing reminder."""
    edited_target = {}

    def _do_edit(data):
        target = None
        for r in data:
            if r["id"] == args.id:
                target = r
                break

        if target is None:
            raise ValueError(f"No reminder found with ID: {args.id}")

        if args.type:
            if args.type not in VALID_TYPES:
                raise ValueError(f"--type must be one of: {', '.join(sorted(VALID_TYPES))}. Got: '{args.type}'")
            target["type"] = args.type
        if args.channel:
            if args.channel not in VALID_CHANNELS:
                raise ValueError(f"--channel must be one of: {', '.join(sorted(VALID_CHANNELS))}. Got: '{args.channel}'")
            target["channel"] = args.channel
        if args.task:
            target["task"] = args.task
        if args.recurring:
            if args.recurring not in VALID_RECURRING:
                raise ValueError(f"--recurring must be one of: {', '.join(sorted(VALID_RECURRING))}. Got: '{args.recurring}'")
            target["recurring"] = args.recurring
        if args.time:
            _validate_time_format(args.time, target["recurring"])
            target["time"] = args.time
        if args.day:
            _validate_day(args.day, target["recurring"])
            target["day"] = args.day.lower()
        if args.source is not None:
            if args.source:
                target["source"] = args.source
            else:
                target.pop("source", None)

        edited_target.update(target)
        return data, None

    _locked_read_modify_write(reminders_path, _do_edit)

    out = StringIO()
    out.write(f"Reminder updated: {edited_target['id']}\n")
    out.write(f"  Type: {edited_target['type']}\n")
    out.write(f"  Channel: {edited_target['channel']}\n")
    out.write(f"  Task: {edited_target['task']}\n")
    out.write(f"  Time: {edited_target['time']}")
    if "day" in edited_target:
        out.write(f" ({edited_target['day']})")
    out.write(f"\n  Recurring: {edited_target['recurring']}\n")
    return out.getvalue()


def cmd_remove(args, reminders_path: Path) -> str:
    """Remove a reminder by ID."""
    def _do_remove(data):
        filtered = [r for r in data if r["id"] != args.id]
        if len(filtered) == len(data):
            raise ValueError(f"No reminder found with ID: {args.id}")
        return filtered, None

    _locked_read_modify_write(reminders_path, _do_remove)
    return f"Reminder removed: {args.id}\n"


def cmd_list(args, reminders_path: Path) -> str:
    """List all reminders."""
    reminders = _read_reminders(reminders_path)

    if args.json:
        return json.dumps(reminders, indent=2, ensure_ascii=False)

    if not reminders:
        return "No reminders configured.\n"

    out = StringIO()
    out.write(f"Reminders ({len(reminders)}):\n\n")

    for r in reminders:
        icon = "🔁" if r["recurring"] != "none" else "📌"
        time_display = r["time"]
        if "day" in r:
            time_display += f" ({r['day']})"

        out.write(f"  {icon} [{r['recurring']}] {time_display} — {r['task'][:80]}\n")
        out.write(f"    ID: {r['id']} | Type: {r['type']} | Channel: {r['channel']}\n")

    return out.getvalue()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="reminder",
        description="Reminder CLI for synapse-engine. Manages reminders in reminders.json.",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to reminders.json (default: auto-resolved from REMINDERS_JSON_PATH env or notes/reminders/reminders.json)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="Add a new reminder")
    p_add.add_argument("--type", required=True, help="Reminder type: 'message' or 'work'")
    p_add.add_argument("--task", required=True, help="The reminder text or work description")
    p_add.add_argument("--time", required=True,
                       help="Fire time. For one-shot: full ISO datetime (e.g., 2026-06-22T07:00:00). "
                            "For recurring: HH:MM in 24h format (e.g., 07:00, 15:30)")
    p_add.add_argument("--recurring", default="none",
                       help="Recurrence: none, daily, weekly, weekdays, monthly (default: none)")
    p_add.add_argument("--day", default="",
                       help="Day context. For weekly: weekday name (e.g., friday). "
                            "For monthly: day number (1-31) or 'last'. Not used for daily/weekdays/none.")
    p_add.add_argument("--channel", default="telegram",
                       help="Delivery channel: telegram or email (default: telegram)")
    p_add.add_argument("--source", default="",
                       help="Optional vault source reference (e.g., daily/2026-04-12)")

    # edit
    p_edit = sub.add_parser("edit", help="Edit an existing reminder")
    p_edit.add_argument("--id", required=True, help="Reminder ID (UUID)")
    p_edit.add_argument("--type", default="", help="New type: 'message' or 'work'")
    p_edit.add_argument("--task", default="", help="New task text")
    p_edit.add_argument("--time", default="", help="New fire time")
    p_edit.add_argument("--recurring", default="", help="New recurrence: none, daily, weekly, weekdays, monthly")
    p_edit.add_argument("--day", default="", help="New day context")
    p_edit.add_argument("--channel", default="", help="New channel: telegram or email")
    p_edit.add_argument("--source", default=None, help="New source reference (empty string to clear)")

    # remove
    p_remove = sub.add_parser("remove", help="Remove a reminder")
    p_remove.add_argument("--id", required=True, help="Reminder ID (UUID)")

    # list
    p_list = sub.add_parser("list", help="List all reminders")
    p_list.add_argument("--json", action="store_true", help="Output as raw JSON")

    args = parser.parse_args(argv)
    reminders_path = _resolve_reminders_path(args.config)

    try:
        if args.command == "add":
            result = cmd_add(args, reminders_path)
        elif args.command == "edit":
            result = cmd_edit(args, reminders_path)
        elif args.command == "remove":
            result = cmd_remove(args, reminders_path)
        elif args.command == "list":
            result = cmd_list(args, reminders_path)
        else:
            parser.print_help()
            return

        print(result)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
