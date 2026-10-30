"""Tiny cooldown circuit breaker for Redis-backed *protections* (Phase 34).

Problem measured by the chaos run: when Redis hangs instead of refusing, every call waits the
full socket timeout (2 s). A login does two Redis operations, so each login paid ~4 s even
though the throttle was configured to fail open. After one failure the breaker stays open for
`cooldown_seconds`, during which callers skip Redis and apply their fail-open/closed policy
immediately; after the cooldown the next call is a real attempt (a success closes the breaker).

Per process, not shared across replicas, and intentionally minimal: it bounds latency during an
outage; it does not decide *what* to do (that stays with the caller's fail-open setting).
"""

from __future__ import annotations

import time
from collections.abc import Callable


class CooldownBreaker:
    def __init__(
        self, cooldown_seconds: float = 10.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def trip(self) -> bool:
        """Open the breaker. Returns True if this call opened it (False if already open)."""
        was_open = self.is_open
        self._open_until = self._clock() + self._cooldown
        return not was_open

    def reset(self) -> None:
        self._open_until = 0.0
