"""
Sliding-window rate limiter shared across all ingestion channels.
"""

import time
from collections import deque


class RateLimiter:
    """Sliding-window rate limiter."""

    def __init__(self, max_events: int, window_seconds: int):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        """Return True if the event is allowed, False if rate-limited."""
        now = time.time()
        # Purge expired timestamps
        while self._timestamps and (now - self._timestamps[0]) > self.window_seconds:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_events:
            return False
        self._timestamps.append(now)
        return True
