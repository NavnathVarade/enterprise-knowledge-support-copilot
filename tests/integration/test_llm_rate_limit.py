from __future__ import annotations

import uuid

import fakeredis.aioredis
import pytest
from packages.domain.errors import RateLimitExceeded
from packages.llm.rate_limit import RedisRateLimiter
from redis.exceptions import ConnectionError as RedisConnectionError

pytestmark = pytest.mark.integration


async def test_limit_enforced_per_tenant() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rl = RedisRateLimiter(redis, limit_per_window=2)
    a, b = uuid.uuid4(), uuid.uuid4()
    await rl.check(a)
    await rl.check(a)
    with pytest.raises(RateLimitExceeded):
        await rl.check(a)
    await rl.check(b)  # other tenant unaffected


class _Broken:
    async def incr(self, key: str) -> int:
        raise RedisConnectionError("down")

    async def expire(self, key: str, s: int) -> None: ...


async def test_redis_failure_fail_open_and_fail_closed() -> None:
    await RedisRateLimiter(_Broken(), limit_per_window=1).check(uuid.uuid4())  # type: ignore[arg-type]
    closed = RedisRateLimiter(_Broken(), limit_per_window=1, fail_open=False)  # type: ignore[arg-type]
    with pytest.raises(RateLimitExceeded):
        await closed.check(uuid.uuid4())


def test_invalid_config() -> None:
    with pytest.raises(ValueError):
        RedisRateLimiter(fakeredis.aioredis.FakeRedis(), limit_per_window=0)
