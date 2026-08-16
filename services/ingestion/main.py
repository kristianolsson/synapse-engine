"""
Unified entry point for the synapse-engine ingestion service.

Starts enabled services (channels + tools that need a background thread)
in separate threads with a shared rate limiter. Configurable via
ENABLED_SERVICES env var.
"""

import logging
import signal
import sys
import threading
from pathlib import Path

from . import config
from .core.rate_limiter import RateLimiter
from .registry import ServiceRegistry, RegistryError
from .vault_sync import apply, commit_and_push_if_changed

logger = logging.getLogger(__name__)

SERVICES_DIR = Path(__file__).resolve().parent / "services"


def validate_startup(registry: ServiceRegistry, enabled: set) -> None:
    """Raises RegistryError with a specific message on any misconfiguration."""
    registry.validate_enabled(enabled)


def sync_vault(registry: ServiceRegistry, enabled: set, vault_path: Path, services_dir: Path) -> None:
    """Applies vault protocol templates and commits/pushes any changes.

    Never raises — runs at boot before any listener thread starts, so a
    failure here (e.g. a misconfigured VAULT_PATH) must not crash the
    process, which under a supervisor means a crash loop. Matches
    commit_and_push_if_changed's own fail-soft contract.
    """
    try:
        changed_paths = apply(registry, enabled, vault_path, services_dir)
        if changed_paths:
            logger.info("apply(): vault protocol files updated, committing...")
            commit_and_push_if_changed(vault_path, changed_paths, push=True)
    except Exception as e:
        logger.error(
            "apply() failed (VAULT_PATH=%s): %s — continuing without vault sync so listeners can still start",
            vault_path, e,
        )


def main():
    """CLI entry point for the ingestion service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    registry = ServiceRegistry.discover(SERVICES_DIR)
    enabled = config.ENABLED_SERVICES

    try:
        validate_startup(registry, enabled)
    except RegistryError as e:
        logger.error("Startup validation failed: %s", e)
        sys.exit(1)

    vault_path = Path(config.VAULT_PATH)
    sync_vault(registry, enabled, vault_path, SERVICES_DIR)

    logger.info("Enabled services: %s", ", ".join(sorted(enabled)))

    rate_limiter = RateLimiter(
        config.RATE_LIMIT_MAX, config.RATE_LIMIT_WINDOW_SECONDS
    )

    listeners = []
    threads = []

    if "email" in enabled:
        from .services.email.listener import EmailListener

        email_listener = EmailListener(rate_limiter=rate_limiter)
        listeners.append(email_listener)
        t = threading.Thread(target=email_listener.run, name="email-listener", daemon=True)
        threads.append(t)
        logger.info("Email channel enabled.")

    if "telegram" in enabled:
        from .services.telegram.listener import TelegramListener

        telegram_listener = TelegramListener(rate_limiter=rate_limiter)
        listeners.append(telegram_listener)
        t = threading.Thread(target=telegram_listener.run, name="telegram-listener", daemon=True)
        threads.append(t)
        logger.info("Telegram channel enabled.")

    if "reminder" in enabled:
        from .services.reminder.scheduler import ReminderScheduler

        scheduler = ReminderScheduler()
        listeners.append(scheduler)
        t = threading.Thread(target=scheduler.run, name="reminder-scheduler", daemon=True)
        threads.append(t)
        logger.info("Reminder scheduler enabled.")

    def signal_handler(sig, frame):
        logger.info("Shutdown signal received, stopping all services...")
        for listener in listeners:
            listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
