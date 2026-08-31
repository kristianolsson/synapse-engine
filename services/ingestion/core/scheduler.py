"""
Reminder scheduler for synapse-engine.

Two-tier architecture:
  1. Scan loop (runs at X:59 each hour): reads reminders.json directly,
     computes next fire times, and updates the in-memory priority queue.
  2. Dispatch loop (event-driven): fires reminders at their exact scheduled
     time using a heapq priority queue.

Dispatch behavior:
  - "message" type → send directly to user (no LLM call).
  - "work" type → pipe through normal ingestion as if from user.
  - On delivery failure → fallback to AI to log to master_todos.
  - On one-shot removal failure → send email alert.
"""

import calendar as cal_mod
import fcntl
import heapq
import json
import logging
import os
import threading
import time
import subprocess
from datetime import datetime, timedelta
from typing import Optional

from .. import config
from ..core.pipe import IncomingMessage, sync_and_build_prompt, pipe_to_provider
from ..core.session_manager import SessionManager, UserSession
from ..providers.base import GLOBAL_PROVIDER_LOCK

logger = logging.getLogger(__name__)

LOCAL_TZ = config.LOCAL_TZ

# Threshold in seconds: if a reminder is less than this late, don't prefix with "missed"
MISSED_THRESHOLD_SECONDS = 300  # 5 minutes

FALLBACK_PROMPT_TEMPLATE = (
    "Delivery of a scheduled reminder failed. "
    "Please log the following task to tasks/master_todos.md under the appropriate section "
    "so it is not lost:\n\n"
    "Task: {task}"
)

WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def compute_next_fire(reminder: dict, after: Optional[datetime] = None) -> Optional[datetime]:
    """Compute the next fire time for a reminder.

    Args:
        reminder: A reminder dict from reminders.json.
        after: Reference time (default: now). The next fire time will be
               strictly after this time for recurring reminders, or the
               exact stored time for one-shot reminders.

    Returns:
        A timezone-aware datetime in America/Los_Angeles, or None if the
        reminder cannot fire (e.g., invalid config).
    """
    if after is None:
        after = datetime.now(LOCAL_TZ)

    recurring = reminder.get("recurring", "none")
    time_str = reminder.get("time", "")

    if recurring == "none":
        # One-shot: parse the full ISO datetime
        try:
            dt = datetime.fromisoformat(time_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt
        except (ValueError, TypeError):
            logger.error("Invalid one-shot time format: %s", time_str)
            return None

    # Recurring: parse HH:MM
    try:
        parts = time_str.split(":")
        hour, minute = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        logger.error("Invalid recurring time format: %s", time_str)
        return None

    if recurring == "hourly":
        candidate = after.replace(minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(hours=1)
        return candidate

    elif recurring == "daily":
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    elif recurring == "weekly":
        day_name = reminder.get("day", "").lower()
        target_weekday = WEEKDAY_MAP.get(day_name)
        if target_weekday is None:
            logger.error("Invalid weekday for weekly reminder: %s", day_name)
            return None

        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        current_weekday = after.weekday()
        days_ahead = (target_weekday - current_weekday) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= after:
            candidate += timedelta(weeks=1)
        return candidate

    elif recurring == "weekdays":
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        # Advance to next weekday (Mon=0..Fri=4)
        while candidate.weekday() > 4:
            candidate += timedelta(days=1)
        return candidate

    elif recurring == "monthly":
        day_str = reminder.get("day", "1").lower()

        def _monthly_candidate(year: int, month: int) -> datetime:
            if day_str == "last":
                day_num = cal_mod.monthrange(year, month)[1]
            else:
                day_num = min(int(day_str), cal_mod.monthrange(year, month)[1])
            return datetime(year, month, day_num, hour, minute, 0, tzinfo=LOCAL_TZ)

        # Try current month first
        candidate = _monthly_candidate(after.year, after.month)
        if candidate <= after:
            # Move to next month
            if after.month == 12:
                candidate = _monthly_candidate(after.year + 1, 1)
            else:
                candidate = _monthly_candidate(after.year, after.month + 1)
        return candidate

    else:
        logger.error("Unknown recurring type: %s", recurring)
        return None


class ReminderScheduler:
    """
    Two-tier reminder scheduler.

    - Scan loop: reads reminders.json hourly at X:59 to pick up new/changed/deleted reminders.
    - Dispatch loop: fires reminders at their exact scheduled time.
    """

    def __init__(self, session_manager: SessionManager):
        # Must be the caller's live instance, not a fresh SessionManager() —
        # per-user /stats preferences are in-memory only, so a new instance
        # would never see toggles set via the other channels (see main.py,
        # which constructs exactly one shared instance for this reason).
        self.session_manager = session_manager
        self._running = False
        self._stop_event = threading.Event()

        # Priority queue: list of (fire_time, reminder_id, reminder_dict)
        self._heap: list[tuple[datetime, str, dict]] = []
        self._heap_lock = threading.Lock()

        # Set by scan loop to wake up dispatch loop
        self._wake_event = threading.Event()

        # Track which reminder IDs are currently in the heap (with their fire times and snapshots)
        self._scheduled_ids: dict[str, tuple[datetime, dict]] = {}

    # ── JSON I/O ─────────────────────────────────────────────────────────

    def _lock_path(self) -> str:
        """Return the lockfile path for reminders.json."""
        return config.REMINDERS_JSON_PATH + ".lock"

    def _read_reminders_json(self) -> list[dict] | None:
        """Read reminders.json. Returns None on error (distinct from empty [])."""
        path = config.REMINDERS_JSON_PATH
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError) as e:
            logger.error("Failed to read reminders.json: %s", e)
            self._send_email(
                text=f"Failed to read reminders.json. The scheduler cannot pick up new reminders until this is resolved.\n\nError: {e}",
                subject="[Synapse Alert] Failed to read reminders.json",
            )
            return None

    def _remove_from_json(self, reminder_id: str) -> bool:
        """Remove a reminder from reminders.json by ID. Returns True on success.

        Uses a lockfile to serialize access with the CLI tool, and GLOBAL_PROVIDER_LOCK
        to prevent deadlocks with concurrent LLM executions (which might use git).
        """
        logger.debug("Waiting for GLOBAL_PROVIDER_LOCK in _remove_from_json...")
        with GLOBAL_PROVIDER_LOCK:
            logger.debug("Acquired GLOBAL_PROVIDER_LOCK in _remove_from_json.")
            path = config.REMINDERS_JSON_PATH
            tmp_path = path + ".tmp"
            lock_path = self._lock_path()
            removed = False
            try:
                with open(lock_path, "w") as lf:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                    try:
                        with open(path, "r") as f:
                            data = json.load(f)

                        if not isinstance(data, list):
                            return False

                        original_count = len(data)
                        data = [r for r in data if r.get("id") != reminder_id]
                        if len(data) == original_count:
                            return False

                        with open(tmp_path, "w") as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                            f.write("\n")
                            f.flush()
                            os.fsync(f.fileno())
                        os.rename(tmp_path, path)
                        removed = True
                    finally:
                        fcntl.flock(lf, fcntl.LOCK_UN)
                
                if removed:
                    # Automatically commit and push to keep git in sync
                    self._sync_reminders_to_git()
                return removed
            except Exception as e:
                logger.error("Failed to remove reminder %s from JSON: %s", reminder_id, e)
                self._send_email(
                    text=f"Failed to remove fired one-shot reminder from reminders.json.\n\n"
                         f"Reminder ID: {reminder_id}\nError: {e}\n\n"
                         f"The reminder may fire again on next restart. Please remove it manually.",
                    subject="[Synapse Alert] Failed to clean up one-shot reminder",
                )
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                return False

    def _sync_reminders_to_git(self) -> None:
        """Commit and push reminders.json to git after modifications.
        Must be called with GLOBAL_PROVIDER_LOCK already acquired.
        """
        path = config.REMINDERS_JSON_PATH
        vault_dir = os.path.dirname(path)
        try:
            # Only proceed with commit if there are changes to the file
            status = subprocess.run(["git", "status", "--porcelain", path], cwd=vault_dir, check=True, capture_output=True, text=True)
            if not status.stdout.strip():
                return
            subprocess.run(["git", "add", path], cwd=vault_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Auto-sync reminders.json (one-shot cleanup)"], cwd=vault_dir, check=True, capture_output=True)
            # Pull with rebase now that our changes are safely committed
            subprocess.run(["git", "pull", "--rebase"], cwd=vault_dir, check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=vault_dir, check=True, capture_output=True)
            logger.info("Successfully synced reminders.json to git.")
        except subprocess.CalledProcessError as e:
            err_output = getattr(e, 'stderr', b'').decode('utf-8', errors='ignore')
            logger.error("Failed to git sync reminders.json: %s\n%s", e, err_output)
            self._send_email(
                text=f"Failed to git sync reminders.json after one-shot cleanup.\n\nError: {e}\n\nStderr:\n{err_output}\n\nPlease pull or push manually.",
                subject="[Synapse Alert] Git sync failed for reminders.json",
            )

    # ── Schedule Management ──────────────────────────────────────────────

    def _scan_and_schedule(self) -> None:
        """Read reminders.json and update the in-memory schedule.

        Adds new reminders, removes deleted ones, and updates changed ones.
        """
        now = datetime.now(LOCAL_TZ)
        reminders = self._read_reminders_json()

        if reminders is None:
            logger.warning("Skipping scan — could not read reminders.json.")
            return

        current_ids = {r.get("id") for r in reminders if r.get("id")}

        with self._heap_lock:
            # Remove reminders that are no longer in JSON
            removed_ids = set(self._scheduled_ids.keys()) - current_ids
            if removed_ids:
                self._heap = [
                    (ft, rid, rdict) for ft, rid, rdict in self._heap
                    if rid not in removed_ids
                ]
                heapq.heapify(self._heap)
                for rid in removed_ids:
                    del self._scheduled_ids[rid]
                logger.info("Removed %d deleted reminder(s) from schedule.", len(removed_ids))

            # Add/update reminders
            new_count = 0
            updated_count = 0
            for reminder in reminders:
                rid = reminder.get("id")
                if not rid:
                    continue

                fire_time = compute_next_fire(reminder, after=now)
                if fire_time is None:
                    continue

                if rid in self._scheduled_ids:
                    # Check if anything changed (fire time or content)
                    old_fire_time, old_reminder = self._scheduled_ids[rid]
                    if old_fire_time == fire_time and old_reminder == reminder:
                        continue
                    # Remove old entry and re-add
                    self._heap = [
                        (ft, r_id, rdict) for ft, r_id, rdict in self._heap
                        if r_id != rid
                    ]
                    heapq.heapify(self._heap)
                    updated_count += 1
                else:
                    new_count += 1

                heapq.heappush(self._heap, (fire_time, rid, reminder))
                self._scheduled_ids[rid] = (fire_time, reminder)

            if new_count > 0:
                logger.info("Scheduled %d new reminder(s).", new_count)
            if updated_count > 0:
                logger.info("Updated %d reminder(s).", updated_count)

        # Wake the dispatch loop to re-evaluate sleep time
        self._wake_event.set()

    # ── Delivery ─────────────────────────────────────────────────────────

    def _send_telegram(self, text: str, reply_markup=None) -> Optional[int]:
        """Send a message via Telegram. Returns the message_id on success."""
        from ..channels.telegram.sender import send_telegram_message
        from ..utils.html_utils import sanitize_telegram_html

        if not config.TELEGRAM_ALLOWED_USER_IDS:
            logger.error("No TELEGRAM_ALLOWED_USER_IDS configured, cannot send reminder")
            return None

        chat_id = config.TELEGRAM_ALLOWED_USER_IDS[0]
        return send_telegram_message(chat_id, sanitize_telegram_html(text), reply_markup=reply_markup)

    def _send_email(self, text: str, subject: str, session_id: str = None) -> bool:
        """Send a message via Email. Never raises — callers (e.g. delivery-
        failure alerting) depend on this always returning, so any error here
        is logged and swallowed rather than propagated."""
        try:
            from ..channels.email.reply import send_reply

            to_addr = config.REPLY_TO_ADDRESS or (
                config.ALLOWED_SENDERS[0] if config.ALLOWED_SENDERS else ""
            )
            if not to_addr:
                logger.error("No REPLY_TO_ADDRESS or ALLOWED_SENDERS configured, cannot send reminder")
                return False

            message_id = f"<{session_id}@synapse.local>" if session_id else ""
            return send_reply(
                to_addr=to_addr,
                subject=subject,
                body=text,
                message_id=message_id,
            )
        except Exception as e:
            logger.error("Failed to send email (subject=%r): %s", subject, e)
            return False

    def _make_subject(self, text: str, prefix: str = "Reminder") -> str:
        """Generate a concise email subject from the task/message text."""
        first_line = text.split("\n")[0].strip()
        if len(first_line) > 60:
            first_line = first_line[:57] + "..."
        return f"{prefix}: {first_line}"

    def _deliver(self, channel: str, text: str, subject: str = "", session_id: str = None, reply_markup=None):
        """Deliver a message via the specified channel.

        Returns the Telegram message_id on success for that channel (so callers
        can attach it to Actionable Form state), or a bool for other channels.
        """
        if channel == "telegram":
            if subject:
                text = f"<b>{subject}</b>\n\n{text}"
            msg_id = self._send_telegram(text, reply_markup=reply_markup)
            if msg_id and session_id:
                self.session_manager.save_message_session(msg_id, session_id)
            return msg_id
        elif channel == "email":
            return self._send_email(text, subject=subject, session_id=session_id)
        else:
            logger.error("Unknown delivery channel: %s", channel)
            return False

    # ── Reminder Handlers ────────────────────────────────────────────────

    def _handle_message_reminder(self, channel: str, task: str, is_missed: bool = False, subject_override: str = None) -> None:
        """Send a message reminder directly to the user (no LLM call)."""
        prefix = "⏰ Missed reminder: " if is_missed else "Reminder: " if channel == "telegram" else ""
        text = f"{prefix}{task}"

        if channel == "email":
            subject = subject_override or self._make_subject(task, prefix="⏰ Missed Reminder" if is_missed else "Reminder")
        else:
            subject = subject_override or ""

        success = self._deliver(
            channel,
            text,
            subject=subject,
        )
        if not success:
            self._handle_delivery_failure(task, error=f"Failed to deliver message via {channel}.")

    def _handle_work_reminder(self, channel: str, task: str, is_missed: bool = False, seconds_late: float = 0, subject_override: str = None) -> None:
        """Re-pipe a work reminder through the normal ingestion flow.

        Builds an IncomingMessage as if it came from the user, pipes it
        to the AI provider, and delivers the response.
        """
        # If significantly late, give the AI timing context so it can adjust
        # (e.g., a "check this weekend's F1 race" task firing mid-weekend)
        actual_task = task
        if is_missed:
            hours_late = seconds_late / 3600
            if hours_late >= 24:
                late_desc = f"{hours_late / 24:.0f} day(s)"
            elif hours_late >= 1:
                late_desc = f"{hours_late:.0f} hour(s)"
            else:
                late_desc = f"{seconds_late / 60:.0f} minute(s)"
            actual_task = f"[Context: this task is from a scheduled reminder that was {late_desc} late] {task}"

        # Determine sender identity based on channel
        if channel == "telegram":
            sender = str(config.TELEGRAM_ALLOWED_USER_IDS[0]) if config.TELEGRAM_ALLOWED_USER_IDS else "system"
        else:
            sender = config.REPLY_TO_ADDRESS or (
                config.ALLOWED_SENDERS[0] if config.ALLOWED_SENDERS else "system"
            )
        session = UserSession(self.session_manager, sender)
        source_type = "scheduled_work"

        incoming = IncomingMessage(
            source_type=source_type,
            sender=sender,
            subject="Scheduled Work Task",
            body=actual_task,
        )

        prompt = sync_and_build_prompt(incoming)

        # Precompute the email subject up front (not just at delivery time)
        # so it can ride along in extra_env: if this task hits an E*TRADE
        # auth wall and falls back to a PIN-retry prompt, the retry result
        # needs this same subject to reply with instead of a generic
        # "Synapse: E*TRADE retry result" fallback (see etrade_pin_auth.py).
        email_subject = subject_override or self._make_subject(task, prefix="Synapse")

        extra_env = {"SYNAPSE_CHANNEL": channel, "SYNAPSE_REMINDER_TASK": task}
        if channel == "email":
            extra_env["SYNAPSE_EMAIL_TO"] = sender
            extra_env["SYNAPSE_EMAIL_SUBJECT"] = email_subject

        # Use the stronger work model for scheduled tasks that modify files.
        # Pass intent-based model so each provider can resolve its own best model
        result = pipe_to_provider(
            prompt, model="work",
            extra_env=extra_env,
        )

        if result.is_error and 'quota' in result.output.lower():
            alt_provider = config.get_next_provider(config.get_ai_provider())
            if alt_provider:
                logger.warning("Work reminder failed due to quota. Falling back to %s...", alt_provider)
                result = pipe_to_provider(prompt, model="work", provider_name=alt_provider)

        if result.is_error:
            logger.error("Work reminder failed: %s", result.output)
            self._handle_delivery_failure(task, error=result.output)
            return

        if not result.requires_reply:
            logger.info("Work reminder completed silently (no reply required): %s", task)
            return

        response_text = result.output
        if not response_text:
            response_text = f"✓ Scheduled task completed: {task}"

        if is_missed:
            response_text = f"⏰ _Missed reminder (firing late):_\n\n{response_text}"

        if channel == "telegram":
            from ..utils.stats_formatter import append_stats_telegram
            response_text = append_stats_telegram(response_text, result.stats, session)
        elif channel == "email":
            from ..utils.stats_formatter import append_stats_email
            response_text = append_stats_email(response_text, result.stats, session)

        # Save the session context for future replies if this is an email thread.
        # This allows the user to reply to the summary and continue the conversation.
        # daily_reset=False: same rationale as channels/email/listener.py's own
        # thread sessions — a reply may land a day or more later, and this
        # session should live purely by SESSION_TTL_MINUTES, not also reset
        # at the next midnight boundary.
        if channel == "email" and result.session_id:
            session_key = f"<{result.session_id}@synapse.local>"
            self.session_manager.save_session(session_key, result.session_id, daily_reset=False)

        if channel == "email":
            subject = email_subject
        else:
            subject = subject_override or ""

        # Detect an Actionable Form or task checklist and build the matching
        # keyboard. Uses the same shared dispatch as the Telegram listener —
        # this is a separate delivery path for scheduled reminders, so it
        # can't reuse the listener's code directly, but it must stay behind
        # the same helper rather than its own copy (a prior copy here is
        # exactly what let this go stale and stop rendering forms).
        keyboard = None
        form_id = None
        deliver_subject = subject
        if channel == "telegram":
            from ..channels.telegram.reply_dispatch import build_reply_keyboard, attach_form_message_id

            dispatch_text = response_text
            if subject:
                # Bake the subject in ourselves: _deliver() only prefixes the
                # subject onto the text it actually sends, but a form's cached
                # copy (used to re-render as fields get answered) needs it too.
                dispatch_text = f"<b>{subject}</b>\n\n{dispatch_text}"
                deliver_subject = ""
            chat_id = config.TELEGRAM_ALLOWED_USER_IDS[0] if config.TELEGRAM_ALLOWED_USER_IDS else None
            response_text, keyboard, form_id = build_reply_keyboard(chat_id, sender, dispatch_text)

        msg_id = self._deliver(
            channel,
            response_text,
            subject=deliver_subject,
            session_id=result.session_id,
            reply_markup=keyboard,
        )
        if not msg_id:
            self._handle_delivery_failure(task, error=f"Task completed but failed to deliver the response via {channel}.")
            if form_id:
                from ..core import form_state
                form_state.delete_form(form_id)
        elif form_id:
            from ..channels.telegram.reply_dispatch import attach_form_message_id
            attach_form_message_id(form_id, msg_id, result.session_id)

    def _handle_delivery_failure(self, task: str, error: str = "") -> None:
        """Alert the user and ask AI to log the failed reminder to master_todos.

        This ensures no reminder is silently lost, and the user is always
        told when a scheduled task fails, regardless of the failure reason.
        """
        logger.warning("Delivery failed, sending fallback to log task: %s", task)
        self._alert(
            text=f"A scheduled task failed. The original task was:\n\n{task}\n\nError:\n{error or 'No error detail available.'}",
            subject="[Synapse Alert] Scheduled task failed",
        )
        fallback_prompt = FALLBACK_PROMPT_TEMPLATE.format(task=task)
        try:
            result = pipe_to_provider(fallback_prompt)

            if result.is_error and 'quota' in result.output.lower():
                alt_provider = config.get_next_provider(config.get_ai_provider())
                if alt_provider:
                    logger.warning("Fallback logging failed due to quota. Falling back to %s...", alt_provider)
                    result = pipe_to_provider(fallback_prompt, provider_name=alt_provider)

            if result.is_error:
                logger.error("Fallback logging returned an error: %s", result.output)
                self._alert(
                    text=f"Failed to execute and log reminder. The original task was:\n\n{task}\n\nError:\n{result.output}",
                    subject="[Synapse Alert] Failed to execute and log reminder"
                )
        except Exception as e:
            logger.error("Fallback logging also failed: %s", e)
            self._alert(
                text=f"Failed to execute and log reminder. The original task was:\n\n{task}\n\nException:\n{e}",
                subject="[Synapse Alert] Failed to execute and log reminder"
            )

    def _alert(self, text: str, subject: str) -> None:
        """Best-effort alert email — a notification failure must never mask
        or interrupt the error-handling flow that triggered it (e.g. logging
        the failed task to master_todos)."""
        try:
            self._send_email(text=text, subject=subject)
        except Exception as e:
            logger.error("Failed to send alert email (subject=%r): %s", subject, e)

    # ── Dispatch ─────────────────────────────────────────────────────────

    def _fire_reminder(self, reminder: dict, fire_time: datetime) -> None:
        """Fire a single reminder and handle post-fire bookkeeping."""
        now = datetime.now(LOCAL_TZ)
        rid = reminder.get("id", "unknown")
        reminder_type = reminder.get("type", "message")
        channel = reminder.get("channel", "telegram")
        task = reminder.get("task", "")
        recurring = reminder.get("recurring", "none")

        # Determine if this is a missed reminder (more than threshold late)
        seconds_late = (now - fire_time).total_seconds()
        is_missed = seconds_late > MISSED_THRESHOLD_SECONDS

        logger.info(
            "Firing reminder %s [%s/%s] (scheduled=%s, late=%.0fs%s): %s",
            rid, reminder_type, channel, fire_time.isoformat(),
            seconds_late, " MISSED" if is_missed else "", task[:80],
        )

        subject = reminder.get("subject")
        if subject:
            subject = subject.replace("{date}", datetime.now().strftime("%Y-%m-%d"))

        try:
            if reminder_type == "message":
                self._handle_message_reminder(channel, task, is_missed=is_missed, subject_override=subject)
            elif reminder_type == "work":
                self._handle_work_reminder(channel, task, is_missed=is_missed, seconds_late=seconds_late, subject_override=subject)
            else:
                logger.error("Unknown reminder type: %s", reminder_type)
        except Exception as e:
            logger.error("Error firing reminder %s: %s", rid, e, exc_info=True)
            self._handle_delivery_failure(task, error=str(e))

        # Post-fire: handle recurring vs one-shot
        if recurring == "none":
            # Remove one-shot from JSON (_remove_from_json sends email on failure)
            if not self._remove_from_json(rid):
                logger.warning("One-shot reminder %s not found in reminders.json (may have been removed already).", rid)
            with self._heap_lock:
                self._scheduled_ids.pop(rid, None)
        else:
            # Recurring: compute next fire time and re-schedule
            next_fire = compute_next_fire(reminder, after=now)
            if next_fire:
                with self._heap_lock:
                    heapq.heappush(self._heap, (next_fire, rid, reminder))
                    self._scheduled_ids[rid] = (next_fire, reminder)
                logger.info("Rescheduled recurring reminder %s for %s", rid, next_fire.isoformat())

    def _dispatch_loop(self) -> None:
        """Main dispatch loop: sleep until next reminder, fire it, repeat."""
        logger.info("Dispatch loop started.")

        while self._running:
            self._wake_event.clear()

            # Get next reminder from heap
            with self._heap_lock:
                if self._heap:
                    next_fire, rid, reminder = self._heap[0]
                else:
                    next_fire = None

            if next_fire is None:
                # Nothing scheduled — wait for scan loop to add something
                logger.debug("No reminders scheduled, waiting for scan...")
                self._wake_event.wait(timeout=3600)
                if self._stop_event.is_set():
                    break
                continue

            now = datetime.now(LOCAL_TZ)
            wait_seconds = (next_fire - now).total_seconds()

            if wait_seconds > 0:
                logger.debug("Next reminder %s in %.0fs at %s", rid, wait_seconds, next_fire.isoformat())
                # Wait until fire time, but can be interrupted by scan loop or stop
                interrupted = self._wake_event.wait(timeout=wait_seconds)
                if self._stop_event.is_set():
                    break
                if interrupted:
                    # Scan loop updated the heap — re-evaluate
                    continue

            # Time to fire: pop from heap
            with self._heap_lock:
                if self._heap and self._heap[0][1] == rid:
                    heapq.heappop(self._heap)
                else:
                    # Item was removed by scan loop while we were waiting
                    continue

            self._fire_reminder(reminder, next_fire)

    # ── Scan Loop ────────────────────────────────────────────────────────

    def _scan_loop(self) -> None:
        """Hourly scan at X:59 to pick up new/changed reminders from JSON."""
        logger.info("Scan loop started.")

        while self._running:
            # Calculate seconds until next X:59
            now = datetime.now(LOCAL_TZ)
            target_minute = 59
            if now.minute < target_minute:
                wait = (target_minute - now.minute) * 60 - now.second
            else:
                # Already past :59, wait until next hour's :59
                wait = (60 - now.minute + target_minute) * 60 - now.second

            if wait > 0:
                logger.debug("Scan loop waiting %ds until next X:59...", wait)
                if self._stop_event.wait(wait):
                    break

            logger.info("Scan loop tick — checking reminders.json for changes...")
            try:
                self._scan_and_schedule()
            except Exception as e:
                logger.error("Scan loop error: %s", e, exc_info=True)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the scheduler in background threads."""
        if self._running:
            logger.warning("Scheduler already running.")
            return
        self._running = True

        # Initial scan on startup (catches missed reminders)
        logger.info("Running initial reminder scan on startup...")
        try:
            self._scan_and_schedule()
        except Exception as e:
            logger.error("Initial scan failed: %s", e, exc_info=True)

        # Start dispatch thread
        dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            name="reminder-dispatch",
            daemon=True,
        )
        dispatch_thread.start()

        # Start scan thread
        scan_thread = threading.Thread(
            target=self._scan_loop,
            name="reminder-scan",
            daemon=True,
        )
        scan_thread.start()

        logger.info("Reminder scheduler started (dispatch + scan threads).")

    def stop(self) -> None:
        """Stop the scheduler."""
        logger.info("Stopping reminder scheduler...")
        self._running = False
        self._stop_event.set()
        self._wake_event.set()  # Wake dispatch loop

    def run(self) -> None:
        """Blocking entry point (compatible with main.py's thread-start pattern).

        Starts dispatch and scan in their own threads, then blocks until stop.
        """
        self._running = True

        # Initial scan on startup
        logger.info("Running initial reminder scan on startup...")
        try:
            self._scan_and_schedule()
        except Exception as e:
            logger.error("Initial scan failed: %s", e, exc_info=True)

        # Start scan in a separate thread
        scan_thread = threading.Thread(
            target=self._scan_loop,
            name="reminder-scan",
            daemon=True,
        )
        scan_thread.start()

        # Run dispatch in the current thread (blocking)
        self._dispatch_loop()
