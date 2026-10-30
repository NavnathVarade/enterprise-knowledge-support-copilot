"""
Outbox relay (Spec §57): moves committed outbox rows to Kafka with at-least-once delivery.

A row is marked published only after the broker ACKNOWLEDGED it (delivery callback), never
after merely enqueueing. Rows are claimed `FOR UPDATE SKIP LOCKED`, so several relay
replicas can run safely. A failed row blocks later rows with the SAME partition key in the
batch, preserving per-document order. Duplicates are possible (crash after ack, before
commit) -- every consumer is idempotent (Redis guard + idempotent writes).

Residual ordering caveat: delivery is asynchronous, so if message N for a key permanently
fails after N+1 was already sent in the same batch, N+1 can be delivered first. The
producer is idempotent with bounded in-flight requests, which preserves order across
broker retries; permanent failure of one message is the only exposure.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.database.models.outbox import OutboxEvent
from packages.database.repositories.outbox import claim_unpublished, mark_failed, mark_published
from packages.events.schemas import EventEnvelope
from packages.kafka.producer import EventProducer
from packages.observability.logging import get_logger
from packages.observability.metrics import OUTBOX_BACKLOG, OUTBOX_EVENTS, OUTBOX_OLDEST_AGE

_log = get_logger(component="outbox_relay")


@dataclass(frozen=True)
class RelayResult:
    claimed: int
    published: int
    failed: int
    skipped_for_ordering: int


class OutboxRelay:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        producer: EventProducer,
        *,
        batch_size: int = 100,
        flush_timeout_seconds: float = 10.0,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._heartbeat = heartbeat or (lambda: None)
        self._sessionmaker = sessionmaker
        self._producer = producer
        self._batch_size = batch_size
        self._flush_timeout = flush_timeout_seconds

    async def run_once(self) -> RelayResult:
        async with self._sessionmaker() as session:
            rows = await claim_unpublished(session, limit=self._batch_size)
            acked: dict[uuid.UUID, str | None] = {}
            failures: dict[uuid.UUID, str] = {}
            blocked: set[str] = set()
            skipped = 0

            def callback_for(event_id: uuid.UUID) -> Any:
                def on_delivery(err: Any, _msg: Any) -> None:
                    acked[event_id] = None if err is None else str(err)

                return on_delivery

            sent: list[uuid.UUID] = []
            for row in rows:
                if row.partition_key in blocked:
                    skipped += 1
                    continue
                try:
                    self._producer.publish(
                        row.topic,
                        EventEnvelope.model_validate(row.envelope),
                        on_delivery=callback_for(row.id),
                    )
                    sent.append(row.id)
                except Exception as exc:  # noqa: BLE001 -- queue full, bad envelope, ...
                    failures[row.id] = f"{type(exc).__name__}: {exc}"
                    blocked.add(row.partition_key)
            if sent:
                await asyncio.to_thread(self._producer.flush, self._flush_timeout)

            by_id = {r.id: r for r in rows}
            published: list[uuid.UUID] = []
            for event_id in sent:
                if event_id not in acked:
                    failures[event_id] = "no delivery report before flush timeout"
                    blocked.add(by_id[event_id].partition_key)
                elif acked[event_id] is not None:
                    failures[event_id] = str(acked[event_id])
                    blocked.add(by_id[event_id].partition_key)
                else:
                    published.append(event_id)
            await mark_published(session, published)
            await mark_failed(session, failures)
            await session.commit()
        OUTBOX_EVENTS.labels("published").inc(len(published))
        OUTBOX_EVENTS.labels("failed").inc(len(failures))
        OUTBOX_EVENTS.labels("skipped_for_ordering").inc(skipped)
        if failures:
            _log.warning("outbox_publish_failures", failed=len(failures), published=len(published))
        return RelayResult(len(rows), len(published), len(failures), skipped)

    async def sample_backlog(self) -> None:
        """Backlog size and oldest-row age; both come from the partial index on unpublished
        rows, so this stays cheap however large the table is."""
        async with self._sessionmaker() as session:
            count, oldest = (
                await session.execute(
                    select(func.count(), func.min(OutboxEvent.created_at)).where(
                        OutboxEvent.published_at.is_(None)
                    )
                )
            ).one()
        OUTBOX_BACKLOG.set(count)
        OUTBOX_OLDEST_AGE.set(0 if oldest is None else (datetime.now(UTC) - oldest).total_seconds())

    async def run_forever(self, stop: asyncio.Event, *, idle_sleep_seconds: float = 0.5) -> None:
        while not stop.is_set():
            self._heartbeat()
            try:
                result = await self.run_once()
                await self.sample_backlog()
            except Exception:  # noqa: BLE001 -- keep the relay alive across DB blips
                _log.exception("outbox_relay_iteration_failed")
                result = RelayResult(0, 0, 1, 0)
            if result.claimed == 0 or result.published == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_sleep_seconds)
