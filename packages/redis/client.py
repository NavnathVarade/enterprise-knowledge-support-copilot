"""
Redis client management.

Redis is never the source of truth (Section 51/100): every caller of this module must
be prepared for `check_redis_connection` to raise and must fall back to Postgres/source
data rather than fail the request. This module only manages connections and exposes a
cheap PING-based readiness check; caching policy (TTL, key construction, ACL-aware cache
keys per Section 52) lives in packages/security/cache.py (Phase 16), not here.
"""

from __future__ import annotations

from functools import lru_cache

import redis.asyncio as redis

from packages.config.settings import RedisSettings


@lru_cache
def _client_for(url: str, socket_timeout: float) -> redis.Redis[str]:
    return redis.from_url(
        url,
        socket_timeout=socket_timeout,
        socket_connect_timeout=socket_timeout,
        decode_responses=True,
    )


def get_redis_client(settings: RedisSettings) -> redis.Redis[str]:
    return _client_for(str(settings.url), settings.socket_timeout_seconds)


async def check_redis_connection(settings: RedisSettings) -> None:
    client = get_redis_client(settings)
    await client.ping()
