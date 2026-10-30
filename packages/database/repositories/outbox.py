from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.outbox import OutboxEvent
from packages.events.schemas import EventEnvelope


async def enqueue_event(session: AsyncSession, *, topic: str, event: EventEnvelope) -> OutboxEvent:
    """Adds the event to the caller's transaction. Does NOT commit: the whole point is that
    it commits (or rolls back) together with the business change."""
    row = OutboxEvent(
        id=event.event_id,
        tenant_id=event.tenant_id,
        topic=topic,
        partition_key=event.partition_key,
        envelope=event.model_dump(mode="json"),
    )
    session.add(row)
    await session.flush()
    return row


async def claim_unpublished(
    session: AsyncSession,
    *,
    limit: int,
    topics: Sequence[str] | None = None,
    max_attempts: int | None = None,
    exclude_ids: Collection[uuid.UUID] = (),
) -> list[OutboxEvent]:
    """Oldest-first batch, locked with FOR UPDATE SKIP LOCKED so several relay replicas
    never publish the same row concurrently. Locks last until the caller's transaction ends.

    The optional filters exist for the development dispatcher (only topics it has a handler
    for, rows that have not exhausted their attempts, rows not in local backoff). The Kafka
    relay calls this with none of them and so behaves exactly as before."""
    query = select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
    if topics is not None:
        query = query.where(OutboxEvent.topic.in_(list(topics)))
    if max_attempts is not None:
        query = query.where(OutboxEvent.attempts < max_attempts)
    if exclude_ids:
        query = query.where(OutboxEvent.id.not_in(list(exclude_ids)))
    result = await session.execute(
        query.order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


async def mark_published(session: AsyncSession, ids: list[uuid.UUID]) -> None:
    if ids:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(published_at=datetime.now(UTC), attempts=OutboxEvent.attempts + 1)
        )


async def mark_failed(
    session: AsyncSession, failures: dict[uuid.UUID, str], *, exhaust_at: int | None = None
) -> None:
    """Record a failed attempt. With `exhaust_at` the row is parked at that attempt count
    (used by the development dispatcher for permanent errors, which retrying cannot fix)."""
    for event_id, error in failures.items():
        attempts = (
            OutboxEvent.attempts + 1
            if exhaust_at is None
            else func.greatest(OutboxEvent.attempts + 1, exhaust_at)
        )
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(attempts=attempts, last_error=error[:1000])
        )
