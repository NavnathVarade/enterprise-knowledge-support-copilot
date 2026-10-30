"""Cooldown breaker and its use by the login throttle and LLM rate limiter (Phase 34)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from packages.domain.errors import RateLimitExceeded
from packages.llm.rate_limit import RedisRateLimiter
from packages.redis.breaker import CooldownBreaker
from packages.security.login_throttle import LoginThrottle, TooManyLoginAttempts
from redis.exceptions import TimeoutError as RedisTimeout


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class HangingRedis:
    """Every call 'times out' (as a hung server would after socket_timeout) and is counted."""

    def __init__(self) -> None:
        self.calls = 0

    async def _fail(self, *_a: Any, **_k: Any) -> Any:
        self.calls += 1
        raise RedisTimeout("timed out")

    mget = incr = expire = delete = _fail


class WorkingRedis:
    def __init__(self) -> None:
        self.calls = 0

    async def mget(self, *_a: Any) -> list[None]:
        self.calls += 1
        return [None, None]

    async def incr(self, *_a: Any) -> int:
        self.calls += 1
        return 1

    async def expire(self, *_a: Any) -> None:
        self.calls += 1


def test_breaker_opens_expires_and_resets() -> None:
    clock = Clock()
    b = CooldownBreaker(cooldown_seconds=10, clock=clock)
    assert not b.is_open
    assert b.trip() is True and b.is_open
    assert b.trip() is False  # already open: no second "transition"
    clock.t += 9.9
    assert b.is_open
    clock.t += 0.2
    assert not b.is_open
    b.trip()
    b.reset()
    assert not b.is_open
    with pytest.raises(ValueError):
        CooldownBreaker(cooldown_seconds=0)


async def test_throttle_skips_hung_redis_after_first_failure_when_failing_open() -> None:
    clock = Clock()
    redis = HangingRedis()
    t = LoginThrottle(
        redis,  # type: ignore[arg-type]
        max_per_account=5,
        max_per_ip=30,
        window_seconds=900,
        fail_open=True,
        breaker=CooldownBreaker(10, clock),
    )
    await t.check("acme", "a@example.com", "1.1.1.1")  # real attempt, fails, trips
    assert redis.calls == 1
    for _ in range(5):
        await t.check("acme", "a@example.com", "1.1.1.1")
        await t.record_failure("acme", "a@example.com", "1.1.1.1")
    assert redis.calls == 1  # Redis not touched while the breaker is open: no 2 s waits
    clock.t += 11
    await t.check("acme", "a@example.com", "1.1.1.1")
    assert redis.calls == 2  # cooldown over: a real attempt again


async def test_throttle_fails_closed_immediately_while_breaker_is_open() -> None:
    redis = HangingRedis()
    t = LoginThrottle(
        redis,  # type: ignore[arg-type]
        max_per_account=5,
        max_per_ip=30,
        window_seconds=900,
        fail_open=False,
        breaker=CooldownBreaker(10, Clock()),
    )
    with pytest.raises(TooManyLoginAttempts):
        await t.check("acme", "a@example.com", "1.1.1.1")
    with pytest.raises(TooManyLoginAttempts):
        await t.check("acme", "a@example.com", "1.1.1.1")
    assert redis.calls == 1  # second refusal needed no Redis round trip


async def test_throttle_recovers_and_closes_breaker_on_success() -> None:
    clock = Clock()
    breaker = CooldownBreaker(10, clock)
    breaker.trip()
    clock.t += 11
    redis = WorkingRedis()
    t = LoginThrottle(
        redis,  # type: ignore[arg-type]
        max_per_account=5,
        max_per_ip=30,
        window_seconds=900,
        breaker=breaker,
    )
    await t.check("acme", "a@example.com", "1.1.1.1")
    assert redis.calls == 1 and not breaker.is_open


async def test_rate_limiter_skips_hung_redis_when_failing_open_and_refuses_when_closed() -> None:
    tenant = uuid.uuid4()
    redis = HangingRedis()
    open_limiter = RedisRateLimiter(
        redis,  # type: ignore[arg-type]
        limit_per_window=10,
        fail_open=True,
        breaker=CooldownBreaker(10, Clock()),
    )
    for _ in range(4):
        await open_limiter.check(tenant)
    assert redis.calls == 1

    redis2 = HangingRedis()
    closed = RedisRateLimiter(
        redis2,  # type: ignore[arg-type]
        limit_per_window=10,
        fail_open=False,
        breaker=CooldownBreaker(10, Clock()),
    )
    for _ in range(3):
        with pytest.raises(RateLimitExceeded):
            await closed.check(tenant)
    assert redis2.calls == 1
