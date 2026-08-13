from __future__ import annotations

import time


class TokenBucket:
    """In-memory token bucket per agent key (Q15): capacity calls, refilling over an hour."""

    def __init__(self, capacity: int, refill_per_second: float):
        self._capacity = max(1, capacity)
        self._refill = refill_per_second
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self._capacity), now))
        tokens = min(self._capacity, tokens + (now - last) * self._refill)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True
        self._buckets[key] = (tokens, now)
        return False
