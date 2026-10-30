"""
Development event dispatch without Kafka (ADR-011), against the real outbox table.

Mechanics tests use a private topic per test, so they never touch other tests' outbox rows.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from packages.config.settings import get_settings
from packages.database.models import Tenant
from packages.database.models.outbox import OutboxEvent
from packages.database.repositories.outbox import enqueue_event
from packages.database.session import get_sessionmaker
from packages.events.schemas import EventEnvelope, EventType
from packages.workers.local_dispatch import LocalEventDispatcher
from sqlalchemy import select

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sessionmaker() -> Any:
    return get_sessionmaker(get_settings().database)


async def _enqueue(sessionmaker: Any, topic: str) -> uuid.UUID:
    async with sessionmaker() as session:
        row = Tenant(name="Local dispatch", slug=f"ld-{uuid.uuid4().hex[:8]}")
        session.add(row)
        await session.commit()
        tenant = row.id
    event = EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=tenant,
        producer="test",
        correlation_id="c",
        trace_id="t",
        partition_key=f"{tenant}:x",
        payload={"n": 1},
    )
    async with sessionmaker() as session:
        await enqueue_event(session, topic=topic, event=event)
        await session.commit()
    return event.event_id


async def _row(sessionmaker: Any, event_id: uuid.UUID) -> OutboxEvent:
    async with sessionmaker() as session:
        return (
            await session.execute(select(OutboxEvent).where(OutboxEvent.id == event_id))
        ).scalar_one()


async def test_a_handled_event_is_marked_published(sessionmaker: Any) -> None:
    topic = f"test.local.{uuid.uuid4().hex}"
    seen: list[EventEnvelope] = []

    async def handler(event: EventEnvelope) -> None:
        seen.append(event)

    event_id = await _enqueue(sessionmaker, topic)
    result = await LocalEventDispatcher(sessionmaker, {topic: handler}).run_once()
    assert (result.handled, result.failed) == (1, 0)
    assert [e.event_id for e in seen] == [event_id]
    row = await _row(sessionmaker, event_id)
    assert row.published_at is not None and row.last_error is None


async def test_events_without_a_local_handler_are_left_untouched(sessionmaker: Any) -> None:
    topic = f"test.nobody.{uuid.uuid4().hex}"
    event_id = await _enqueue(sessionmaker, topic)

    async def other(event: EventEnvelope) -> None:  # pragma: no cover - must not be called
        raise AssertionError

    result = await LocalEventDispatcher(sessionmaker, {"some.other.topic": other}).run_once()
    assert result.handled == 0
    row = await _row(sessionmaker, event_id)
    assert row.published_at is None and row.attempts == 0  # not lost, not "published"


async def test_a_retryable_failure_is_kept_and_retried_with_backoff(sessionmaker: Any) -> None:
    topic = f"test.retry.{uuid.uuid4().hex}"
    calls = 0

    async def flaky(event: EventEnvelope) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("provider down")

    clock = Clock()
    dispatcher = LocalEventDispatcher(
        sessionmaker, {topic: flaky}, retry_base_seconds=10, max_attempts=3, clock=clock
    )
    event_id = await _enqueue(sessionmaker, topic)

    first = await dispatcher.run_once()
    assert (first.handled, first.failed) == (0, 1)
    row = await _row(sessionmaker, event_id)
    assert row.published_at is None and row.attempts == 1
    assert row.last_error and "ConnectionError" in row.last_error

    assert (await dispatcher.run_once()).failed == 0 and calls == 1  # still backing off
    clock.now += 11
    assert (await dispatcher.run_once()).handled == 1 and calls == 2
    assert (await _row(sessionmaker, event_id)).published_at is not None


async def test_a_permanent_failure_is_parked_immediately_and_never_dropped(
    sessionmaker: Any,
) -> None:
    topic = f"test.perm.{uuid.uuid4().hex}"

    class InvalidDocument(Exception):  # name is on the non-retryable list
        pass

    async def bad(event: EventEnvelope) -> None:
        raise InvalidDocument("corrupt")

    dispatcher = LocalEventDispatcher(sessionmaker, {topic: bad}, max_attempts=5)
    event_id = await _enqueue(sessionmaker, topic)
    await dispatcher.run_once()
    row = await _row(sessionmaker, event_id)
    assert row.published_at is None and row.attempts == 5 and "corrupt" in (row.last_error or "")
    again = await dispatcher.run_once()  # parked rows are not claimed again
    assert (again.handled, again.failed) == (0, 0)


async def test_run_forever_stops_promptly_when_asked(sessionmaker: Any) -> None:
    dispatcher = LocalEventDispatcher(sessionmaker, {}, poll_seconds=30)
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run_forever(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
