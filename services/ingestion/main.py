"""
Unified entry point for the synapse-engine ingestion service.

Starts enabled channels (email, telegram, or both) in separate threads
with a shared rate limiter. Configurable via ENABLED_CHANNELS env var.
"""

import logging
import signal
import sys
import threading

from . import config
from .core.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


def main():
    """CLI entry point for the ingestion service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    channels = config.ENABLED_CHANNELS
    if not channels:
        logger.error("No channels enabled. Set ENABLED_CHANNELS in .env")
        sys.exit(1)

    logger.info("Enabled channels: %s", ", ".join(channels))

    # Shared rate limiter across all channels
    rate_limiter = RateLimiter(
        config.RATE_LIMIT_MAX, config.RATE_LIMIT_WINDOW_SECONDS
    )

    listeners = []
    threads = []

    # --- Email ---
    if "email" in channels:
        from .channels.email.listener import EmailListener

        email_listener = EmailListener(rate_limiter=rate_limiter)
        listeners.append(email_listener)

        t = threading.Thread(
            target=email_listener.run,
            name="email-listener",
            daemon=True,
        )
        threads.append(t)
        logger.info("Email channel enabled.")

    # --- Telegram ---
    if "telegram" in channels:
        if not config.TELEGRAM_BOT_TOKEN:
            logger.error(
                "TELEGRAM_BOT_TOKEN not set. Cannot start Telegram channel."
            )
            sys.exit(1)

        from .channels.telegram.listener import TelegramListener

        telegram_listener = TelegramListener(rate_limiter=rate_limiter)
        listeners.append(telegram_listener)

        t = threading.Thread(
            target=telegram_listener.run,
            name="telegram-listener",
            daemon=True,
        )
        threads.append(t)
        logger.info("Telegram channel enabled.")

    # --- Signal handling ---
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received, stopping all channels...")
        for listener in listeners:
            listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # --- Start all threads ---
    for t in threads:
        t.start()

    # Block until all threads exit
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
