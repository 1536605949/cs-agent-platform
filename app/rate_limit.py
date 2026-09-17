"""In-process sliding-window rate limiter.

Deliberately dependency-free: the platform must run with an empty ``.env``, so
there is no Redis requirement. For a horizontally scaled deployment, replace
this module with a shared store (Redis / gateway-level limiting).
"""

from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, limit_per_minute: int = 60):
        self.limit = max(1, limit_per_minute)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Return True when ``key`` is still under the per-minute quota."""
        now = time.monotonic()
        window_start = now - 60.0

        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < window_start:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)

            # Opportunistic cleanup so the dict cannot grow without bound.
            if len(self._hits) > 10_000:
                for stale_key in [k for k, v in self._hits.items() if not v or v[-1] < window_start]:
                    self._hits.pop(stale_key, None)
            return True

    def remaining(self, key: str) -> int:
        with self._lock:
            bucket = self._hits.get(key)
            if not bucket:
                return self.limit
            window_start = time.monotonic() - 60.0
            return max(0, self.limit - sum(1 for ts in bucket if ts >= window_start))
