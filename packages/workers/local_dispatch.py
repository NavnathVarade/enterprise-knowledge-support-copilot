"""
Development-only event dispatcher: runs outbox events WITHOUT Kafka.

Why this shape: the API already commits every domain event to the Postgres `outbox_events`
table in the same transaction as the business change. In production a relay publishes those
rows to Kafka and a consumer group handles them. On a laptop with no broker, this dispatcher
reads the SAME rows and calls the SAME handler in-process, so uploads are really ingested
instead of sitting `pending` forever.

What it guarantees (and what it does not) -- see docs/adr/ADR-011:
  * Durable: the event is a committed Postgres row. Restarting the API loses nothing.
  * At-least-once: a row is marked published only after the handler returned; a crash
    mid-handler re-runs it (the ingestion handler is idempotent on document state).
  * Failures are never dropped: the row stays in the table with `attempts` and `last_error`.
    Retryable errors back off exponentially (in this process) up to a maximum number of
    attempts; permanent errors are parked immediately. Parked rows are visible in SQL.
  * Only topics with a registered handler are touched. Events nobody handles locally stay in
    the table untouched (they are not lost, and a later Kafka relay would still publish them).
  NOT provided: ordering across documents, replay, consumer groups, a DLQ topic, delivery to
  other processes, or exactly-once. It must never run alongside the Kafka relay/consumer;
  settings forbid that combination and forbid it entirely in staging/production.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.database.repositories.outbox import claim_unpublished, mark_failed, mark_published
from packages.events.schemas import EventEnvelope
from packages.kafka.retry import is_retryable
from packages.observability.logging import get_logger

_log = get_logger(component="local_event_dispatch")

Handler = Callable[[EventEnvelope], Awaitable[None]]


@dataclass(frozen=True)
class DispatchResult:
    handled: int = 0
    failed: int = 0


class LocalEventDispatcher:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        handlers: Mapping[str, Handler],
        *,
        poll_seconds: float = 1.0,
        max_attempts: int = 5,
        retry_base_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._handlers = dict(handlers)
        self._poll = poll_seconds
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._clock = clock
        self._not_before: dict[uuid.UUID, float] = {}

    async def run_once(self) -> DispatchResult:
        """Handle every currently-eligible event, one transaction per event."""
        handled = failed = 0
        while True:
            outcome = await self._handle_next()
            if outcome is None:
                return DispatchResult(handled, failed)
            if outcome:
                handled += 1
            else:
                failed += 1

    async def _handle_next(self) -> bool | None:
        """True = handled, False = failed, None = nothing eligible."""
        now = self._clock()
        backoff = [event_id for event_id, until in self._not_before.items() if until > now]
        async with self._sessionmaker() as session:
            rows = await claim_unpublished(
                session,
                limit=1,
                topics=list(self._handlers),
                max_attempts=self._max_attempts,
                exclude_ids=backoff,
            )
            if not rows:
                return None
            row = rows[0]
            try:
                await self._handlers[row.topic](EventEnvelope.model_validate(row.envelope))
            except Exception as exc:  # noqa: BLE001 -- the failure is recorded, not swallowed
                permanent = not is_retryable(type(exc).__name__)
                attempts_after = row.attempts + 1
                await mark_failed(
                    session,
                    {row.id: f"{type(exc).__name__}: {exc}"},
                    exhaust_at=self._max_attempts if permanent else None,
                )
                await session.commit()
                parked = permanent or attempts_after >= self._max_attempts
                if parked:
                    self._not_before.pop(row.id, None)
                else:
                    self._not_before[row.id] = self._clock() + self._retry_base * 2 ** (
                        attempts_after - 1
                    )
                _log.error(
                    "local_event_failed",
                    event_id=str(row.id),
                    topic=row.topic,
                    error_class=type(exc).__name__,
                    attempts=attempts_after,
                    parked=parked,
                )
                return False
            await mark_published(session, [row.id])
            await session.commit()
            self._not_before.pop(row.id, None)
            _log.info("local_event_handled", event_id=str(row.id), topic=row.topic)
            return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 -- keep the loop alive across DB blips
                _log.exception("local_event_dispatch_iteration_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._poll)
