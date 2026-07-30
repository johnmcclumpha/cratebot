"""Token-bucket limiter and a small circuit breaker for flaky external services."""

from __future__ import annotations

import asyncio
import random
import time


class TokenBucket:
    """Simple async token bucket, e.g. ~10 requests/minute for Odesli's free tier."""

    def __init__(self, rate_per_minute: float, burst: int | None = None) -> None:
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = burst if burst is not None else max(1, int(rate_per_minute))
        self._tokens = float(self._capacity)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._updated_at = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate_per_second
            await asyncio.sleep(wait)


class CircuitBreaker:
    """Trips open after `failure_threshold` consecutive failures, resets after `reset_after` seconds."""

    def __init__(self, failure_threshold: int = 5, reset_after: float = 60.0) -> None:
        self._failure_threshold = failure_threshold
        self._reset_after = reset_after
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._reset_after:
            # half-open: calls are allowed through again until the next failure
            # re-opens the breaker (not limited to a single trial call) - fine here
            # since callers are already independently rate-limited (e.g. Odesli's
            # token bucket), so this can't stampede the recovering service.
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = time.monotonic()


async def backoff_sleep(attempt: int, base: float = 0.5, cap: float = 30.0) -> None:
    """Exponential backoff with jitter, attempt starting at 0."""
    delay = min(cap, base * (2**attempt))
    await asyncio.sleep(delay * random.uniform(0.5, 1.5))
