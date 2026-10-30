"""
Proves Section 46's exact requirement -- "same event twice must not cause duplicate
side effects" -- against a real Redis, not a mock.
"""

from __future__ import annotations

import uuid

import pytest
from packages.config.settings import get_settings
from packages.kafka.idempotency import IdempotencyGuard
from packages.redis.client import get_redis_client


@pytest.fixture
def guard() -> IdempotencyGuard:
    settings = get_settings()
    client = get_redis_client(settings.redis)
    return IdempotencyGuard(client, ttl_seconds=60)


@pytest.mark.integration
async def test_first_claim_succeeds(guard: IdempotencyGuard) -> None:
    key = f"test:{uuid.uuid4()}"
    claimed = await guard.claim(key)
    assert claimed is True


@pytest.mark.integration
async def test_second_claim_of_same_key_fails(guard: IdempotencyGuard) -> None:
    """THE core proof: the same event_id claimed twice must only succeed once."""
    key = f"test:{uuid.uuid4()}"
    first = await guard.claim(key)
    second = await guard.claim(key)
    assert first is True
    assert second is False


@pytest.mark.integration
async def test_release_allows_reclaiming(guard: IdempotencyGuard) -> None:
    key = f"test:{uuid.uuid4()}"
    await guard.claim(key)
    await guard.release(key)
    reclaimed = await guard.claim(key)
    assert reclaimed is True


@pytest.mark.integration
async def test_different_keys_do_not_interfere(guard: IdempotencyGuard) -> None:
    key_a = f"test:{uuid.uuid4()}"
    key_b = f"test:{uuid.uuid4()}"
    assert await guard.claim(key_a) is True
    assert await guard.claim(key_b) is True
