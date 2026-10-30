"""
Per-tenant LLM rate limiting (Section 37, 51). Fixed-window counter in Redis.

Redis is never the source of truth: on a Redis failure the limiter FAILS OPEN by default
(availability over cost control, logged loudly) -- configurable to fail closed.
Fixed windows allow up to 2x burst at a window boundary; that is a documented, accepted
trade-off for a counter that costs one INCR.
"""

from __future__ import annotations

import time
import uuid
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from packages.domain.errors import RateLimitExceeded
from packages.observability.logging import get_logger
from packages.redis.breaker import CooldownBreaker

_log = get_logger(component="llm_rate_limit")

# Shared across requests in this process (limiter objects are cheap and may be per-request).
RATE_LIMIT_BREAKER = CooldownBreaker(cooldown_seconds=10.0)


class RateLimiter(Protocol):
    async def check(self, tenant_id: uuid.UUID) -> None: ...


class NoopRateLimiter:
    async def check(self, tenant_id: uuid.UUID) -> None:
        return None


class RedisRateLimiter:
    def __init__(
        self,
        redis: Redis[str],
        *,
        limit_per_window: int,
        window_seconds: int = 60,
        fail_open: bool = True,
        breaker: CooldownBreaker | None = None,
    ) -> None:
        self._breaker = breaker if breaker is not None else RATE_LIMIT_BREAKER
        if limit_per_window <= 0 or window_seconds <= 0:
            raise ValueError("limit and window must be positive")
        self._redis = redis
        self._limit = limit_per_window
        self._window = window_seconds
        self._fail_open = fail_open

    async def check(self, tenant_id: uuid.UUID) -> None:
        if self._breaker.is_open:  # Redis recently failed: skip the wait, apply the policy
            if self._fail_open:
                return
            raise RateLimitExceeded("Rate limiter unavailable.")
        window_id = int(time.time()) // self._window
        key = f"llm:rl:{tenant_id}:{window_id}"  # tenant in key: no cross-tenant sharing
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, self._window * 2)
        except RedisError:
            if self._breaker.trip():
                _log.warning("llm_rate_limit_redis_unavailable", fail_open=self._fail_open)
            if self._fail_open:
                return
            raise RateLimitExceeded("Rate limiter unavailable.") from None
        self._breaker.reset()
        if count > self._limit:
            raise RateLimitExceeded()
