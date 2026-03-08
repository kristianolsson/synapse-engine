"""
Reminder scheduler for synapse-engine.

Runs on a configurable interval (default: hourly) and prompts the AI provider
to check reminders.md for due reminders. Routes results to the appropriate
channel (Telegram or Email).

Two-phase architecture:
  Phase 1: Prompt AI to parse reminders.md → returns JSON array of due items.
  Phase 2a: "message" type → send directly to user.
  Phase 2b: "work" type → re-pipe through normal ingestion as if from user.
  Phase 2c: On delivery failure → send fallback to AI to log to master_todos.
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from .. import config
from ..core.pipe import IncomingMessage, build_prompt, pipe_to_gemini
from ..core.session_manager import SessionManager

logger = logging.getLogger(__name__)

# The scheduler prompt uses the --- metadata block format so Gemini can
# detect it via the Ingestion Protocols and route to the correct handler.
SCHEDULER_PROMPT_TEMPLATE = (
    "---\n"
    "Type: synapse-engine-scheduler\n"
    "---\n\n"
    "The current date and time is: {current_time}\n\n"
    "Check reminders.md for any reminders that are due within this hour "
    "({hour_start} to {hour_end}). "
    "Return ONLY a JSON array. Do not include any other text."
)

FALLBACK_PROMPT_TEMPLATE = (
    "Delivery of a scheduled reminder failed. "
    "Please log the following task to master_todos.md under the appropriate section "
    "so it is not lost:\n\n"
    "Task: {task}"
)


class ReminderScheduler:
    """
    Periodic reminder scheduler.

    Runs in its own thread and prompts the AI provider at a fixed interval
    to check for due reminders.
    """

    def __init__(
        self,
        interval_minutes: int = None,
    ):
        self.interval_minutes = interval_minutes or config.SCHEDULER_INTERVAL_MINUTES
        self.session_manager = SessionManager()
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _build_scheduler_prompt(self) -> str:
        """Build the prompt for the hourly reminder check."""
        now = datetime.now()
        current_time = now.strftime("%Y-%m-%dT%H:%M:%S")
        hour_start = now.strftime("%Y-%m-%dT%H:00:00")
        hour_end = now.strftime("%Y-%m-%dT%H:59:59")
        return SCHEDULER_PROMPT_TEMPLATE.format(
            current_time=current_time,
            hour_start=hour_start,
            hour_end=hour_end,
        )

    def _parse_response(self, output: str) -> list[dict]:
        """
        Parse the AI response into a list of reminder dicts.

        Expected format: [{"type": "message"|"work", "channel": "telegram"|"email", "message"|"task": "..."}]
        """
        if not output or not output.strip():
            return []

        text = output.strip()

        # Strip markdown code fences if present (```json ... ```)
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (fences)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse scheduler response as JSON: %s\nResponse: %s", e, text[:500])
            return []

        if not isinstance(data, list):
            logger.error("Scheduler response is not a JSON array: %s", type(data))
            return []

        # Validate entries
        valid = []
        for item in data:
            if not isinstance(item, dict):
                logger.warning("Skipping non-dict reminder item: %s", item)
                continue
            item_type = item.get("type", "")
            channel = item.get("channel", "telegram")
            if item_type == "message" and "message" in item:
                valid.append({"type": "message", "channel": channel, "message": item["message"]})
            elif item_type == "work" and "task" in item:
                valid.append({"type": "work", "channel": channel, "task": item["task"]})
            else:
                logger.warning("Skipping invalid reminder item: %s", item)

        return valid

    def _send_telegram(self, text: str) -> Optional[int]:
        """Send a message via Telegram. Returns the message_id on success."""
        from ..channels.telegram.sender import send_telegram_message

        if not config.TELEGRAM_ALLOWED_USER_IDS:
            logger.error("No TELEGRAM_ALLOWED_USER_IDS configured, cannot send reminder")
            return None

        chat_id = config.TELEGRAM_ALLOWED_USER_IDS[0]
        return send_telegram_message(chat_id, text)

    def _send_email(self, text: str, subject: str, session_id: str = None) -> bool:
        """Send a message via Email."""
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

    def _make_subject(self, text: str, prefix: str = "Reminder") -> str:
        """Generate a concise email subject from the task/message text."""
        # Strip to first line, then truncate
        first_line = text.split("\n")[0].strip()
        if len(first_line) > 60:
            first_line = first_line[:57] + "..."
        return f"{prefix}: {first_line}"

    def _deliver(self, channel: str, text: str, subject: str = "", session_id: str = None) -> bool:
        """Deliver a message via the specified channel. Returns True on success."""
        if channel == "telegram":
            msg_id = self._send_telegram(text)
            if msg_id and session_id:
                self.session_manager.save_message_session(msg_id, session_id)
            return bool(msg_id)
        elif channel == "email":
            return self._send_email(text, subject=subject, session_id=session_id)
        else:
            logger.error("Unknown delivery channel: %s", channel)
            return False

    def _handle_work_reminder(self, channel: str, task: str) -> None:
        """
        Re-pipe a work reminder through the normal ingestion flow.

        Builds an IncomingMessage as if it came from the user, pipes it
        to the AI provider, and delivers the response.
        """
        # Determine sender identity based on channel
        if channel == "telegram":
            sender = str(config.TELEGRAM_ALLOWED_USER_IDS[0]) if config.TELEGRAM_ALLOWED_USER_IDS else "system"
            source_type = "telegram"
        else:
            sender = config.REPLY_TO_ADDRESS or (
                config.ALLOWED_SENDERS[0] if config.ALLOWED_SENDERS else "system"
            )
            source_type = "email"

        incoming = IncomingMessage(
            source_type=source_type,
            sender=sender,
            subject="Scheduled Task",
            body=task,
        )

        prompt = build_prompt(incoming)
        result = pipe_to_gemini(prompt)

        if result.is_error:
            logger.error("Work reminder failed: %s", result.output)
            self._handle_delivery_failure(task)
            return

        response_text = result.output
        if not response_text:
            response_text = "✓ Scheduled task completed."

        # Save the session context for future replies if this is an email thread.
        # This allows the user to reply to the summary and continue the conversation.
        if channel == "email" and result.session_id:
            session_key = f"<{result.session_id}@synapse.local>"
            self.session_manager.save_session(session_key, result.session_id)

        success = self._deliver(
            channel,
            response_text,
            subject=self._make_subject(task, prefix="Synapse"),
            session_id=result.session_id,
        )
        if not success:
            self._handle_delivery_failure(task)

    def _handle_delivery_failure(self, task: str) -> None:
        """
        Fallback: ask AI to log the failed reminder to master_todos.

        This ensures no reminder is silently lost.
        """
        logger.warning("Delivery failed, sending fallback to log task: %s", task)
        fallback_prompt = FALLBACK_PROMPT_TEMPLATE.format(task=task)
        try:
            pipe_to_gemini(fallback_prompt)
        except Exception as e:
            logger.error("Fallback logging also failed: %s", e)

    def _tick(self) -> None:
        """Execute one scheduler cycle: prompt AI and process results."""
        logger.info("Scheduler tick — checking for due reminders...")

        prompt = self._build_scheduler_prompt()

        # Use a fresh session (no resume) — scheduler prompts are stateless
        result = pipe_to_gemini(prompt)

        # The scheduler executes a stateless prompt simply to evaluate `reminders.md`.
        # The provider might generate a persistent session for this interaction.
        # Since we don't care about this transient conversation, we delete it immediately
        # to prevent log buildup in the CLI state.
        if result.session_id:
            from ..core.pipe import cleanup_session
            cleanup_session(result.session_id)

        if result.is_error:
            logger.error("Scheduler prompt failed: %s", result.output)
            return

        reminders = self._parse_response(result.output)

        if not reminders:
            logger.info("No reminders due this hour.")
            return

        logger.info("Processing %d due reminder(s).", len(reminders))

        import uuid
        for item in reminders:
            try:
                if item["type"] == "message":
                    success = self._deliver(
                        item["channel"],
                        f"Reminder: {item['message']}" if item["channel"] == "telegram" else item["message"],
                        subject=self._make_subject(item["message"]),
                        session_id=None,
                    )
                    if not success:
                        self._handle_delivery_failure(item["message"])
                elif item["type"] == "work":
                    self._handle_work_reminder(item["channel"], item["task"])
            except Exception as e:
                logger.error("Error processing reminder: %s — %s", item, e)
                task_text = item.get("message") or item.get("task", "unknown")
                self._handle_delivery_failure(task_text)

    def _loop(self) -> None:
        """Main scheduler loop. Aligns to X:01 past the hour, then runs hourly."""
        interval_seconds = self.interval_minutes * 60
        logger.info(
            "Reminder scheduler started (interval=%dm)",
            self.interval_minutes,
        )

        # Wait until 1 minute past the next hour before the first tick
        now = datetime.now()
        minutes_until_next_hour = 60 - now.minute
        initial_delay = (minutes_until_next_hour * 60) - now.second + 60  # +60 for :01
        if initial_delay > interval_seconds:
            initial_delay -= 3600  # Already past :01, don't wait a full extra hour
        if initial_delay > 0:
            logger.info(
                "Waiting %d seconds until next X:01 before first tick...",
                initial_delay,
            )
            if self._stop_event.wait(initial_delay):
                return  # Stop was requested during initial wait

        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.error("Scheduler tick error: %s", e, exc_info=True)

            # Sleep until next X:01
            now = datetime.now()
            seconds_until_next = ((60 - now.minute) * 60) - now.second + 60
            if seconds_until_next > 3600:
                seconds_until_next -= 3600
            if self._stop_event.wait(seconds_until_next):
                break  # Stop was requested

    def start(self) -> None:
        """Start the scheduler in a background thread."""
        if self._running:
            logger.warning("Scheduler already running.")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="reminder-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Reminder scheduler thread started.")

    def stop(self) -> None:
        """Stop the scheduler."""
        logger.info("Stopping reminder scheduler...")
        self._running = False
        self._stop_event.set()  # Wake up any sleeping wait immediately
        if self._thread:
            self._thread.join(timeout=5)

    def run(self) -> None:
        """
        Blocking entry point (compatible with main.py's thread-start pattern).

        Equivalent to start() but blocks the calling thread.
        """
        self._running = True
        self._loop()
