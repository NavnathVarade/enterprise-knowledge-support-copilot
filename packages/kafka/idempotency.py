"""
Idempotency guard for Kafka consumers (Section 46: "Consumers must safely handle
duplicates. Test: same event twice must not cause duplicate side effects.").

Backed by Redis SETNX (atomic "set if not exists"): the first consumer to see a given
event_id wins the key and proceeds; any redelivery of the same event_id finds the key
already set and is told to skip. TTL bounds how long we remember an event_id -- long
enough to cover Kafka's realistic redelivery window (a consumer restart, a rebalance),
short enough not to grow Redis memory unboundedly.

This does NOT replace consumer-side idempotent business logic (e.g. a repository using
"get or create" as Phase 6's seed script does) -- it's a fast-path guard that avoids
redoing expensive work on a redelivered event, layered on top of, not instead of,
idempotent writes.
"""

from __future__ import annotations

import redis.asyncio as redis


class IdempotencyGuard:
    def __init__(self, client: redis.Redis[str], *, ttl_seconds: int = 24 * 60 * 60) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    async def claim(self, dedup_key: str) -> bool:
        """
        Returns True if this call is the first to claim `dedup_key` (the caller should
        proceed with processing). Returns False if the key was already claimed (the
        caller should skip -- this is a duplicate delivery).
        """
        was_set = await self._client.set(dedup_key, "1", nx=True, ex=self._ttl_seconds)
        return was_set is True

    async def release(self, dedup_key: str) -> None:
        """
        Explicitly un-claims a key -- used when processing fails in a way that SHOULD
        be retried (e.g. a transient database timeout) rather than silently treated as
        "already handled". Non-retryable failures (Section 50) should NOT call this;
        leaving the claim in place is correct there, since the event is going to the
        DLQ, not being retried as if it were new.
        """
        await self._client.delete(dedup_key)
