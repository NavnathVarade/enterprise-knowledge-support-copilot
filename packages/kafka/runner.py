"""
Consumer runner (Spec §45-47): the polling loop around `handle_message`.

- Manual offset commits, AFTER the message's final outcome (processed, duplicate-skipped,
  or dead-lettered with the DLQ record flushed). A crash before commit means redelivery,
  which the idempotency guard turns into a no-op for work that already succeeded.
- Retries are bounded, in-process, with jittered exponential backoff (`compute_backoff_seconds`).
  The partition is not consumed past the failing message while it backs off, preserving
  per-document order. Max backoff must stay well under `max.poll.interval.ms` (default
  300s; backoff cap is 60s). The `.retry` topic names exist in `Topics` but this runner does
  not use them: a delayed-retry topic pipeline is a documented future option.
- The Kafka client is a Protocol, so the loop's logic is tested with a fake; the real
  `confluent_kafka.Consumer` wiring (`create_confluent_consumer`) is reviewed, NOT executed
  against a broker in this environment.

Known gap: if the DLQ flush does not complete, the runner raises without committing; the
redelivered event is then treated as a duplicate (its idempotency claim stays), so that one
DLQ record can be lost. Mitigation is operational (alert on the runner crash).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from packages.config.settings import KafkaSettings
from packages.events.schemas import EventEnvelope
from packages.kafka.client_config import base_client_config
from packages.kafka.consumer import MessageOutcome, handle_message
from packages.kafka.idempotency import IdempotencyGuard
from packages.kafka.producer import EventProducer
from packages.kafka.retry import compute_backoff_seconds
from packages.observability.logging import get_logger
from packages.observability.metrics import (
    KAFKA_CONSUMER_LAG,
    KAFKA_HANDLER_DURATION,
    KAFKA_MESSAGES,
    KAFKA_RETRIES,
)

_log = get_logger(component="consumer_runner")


class KafkaMessage(Protocol):
    def value(self) -> bytes | None: ...
    def error(self) -> Any: ...
    def topic(self) -> str: ...
    def partition(self) -> int: ...
    def offset(self) -> int: ...


class KafkaConsumerClient(Protocol):
    def subscribe(self, topics: list[str]) -> None: ...
    def poll(self, timeout: float) -> KafkaMessage | None: ...
    def commit(self, message: KafkaMessage, asynchronous: bool = ...) -> Any: ...
    def close(self) -> None: ...


class FatalConsumerError(RuntimeError):
    pass


class ConsumerRunner:
    def __init__(
        self,
        client: KafkaConsumerClient,
        *,
        topic: str,
        handler: Callable[[EventEnvelope], Awaitable[None]],
        idempotency_guard: IdempotencyGuard,
        dlq_producer: EventProducer,
        consumer_group: str,
        max_attempts: int = 5,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._heartbeat = heartbeat or (lambda: None)
        self._client = client
        self._topic = topic
        self._handler = handler
        self._guard = idempotency_guard
        self._dlq = dlq_producer
        self._group = consumer_group
        self._max_attempts = max_attempts
        self._base = backoff_base_seconds
        self._max_backoff = backoff_max_seconds
        self._sleep = sleep
        self._rng = rng
        client.subscribe([topic])

    async def run_once(self, poll_timeout_seconds: float = 1.0) -> MessageOutcome | None:
        message = await asyncio.to_thread(self._client.poll, poll_timeout_seconds)
        if message is None:
            return None
        err = message.error()
        if err is not None:
            if getattr(err, "fatal", lambda: False)():
                raise FatalConsumerError(str(err))
            _log.warning("consumer_poll_error", error=str(err))
            return None
        raw = message.value() or b""

        outcome = MessageOutcome.retry
        started = time.perf_counter()
        for attempt in range(1, self._max_attempts + 1):
            outcome = await handle_message(
                raw,
                main_topic=self._topic,
                handler=self._handler,
                idempotency_guard=self._guard,
                dlq_producer=self._dlq,
                consumer_group=self._group,
                attempt_count=attempt,
                max_attempts=self._max_attempts,
            )
            if outcome is not MessageOutcome.retry:
                break
            KAFKA_RETRIES.labels(self._topic).inc()
            await self._sleep(
                compute_backoff_seconds(
                    attempt,
                    base_seconds=self._base,
                    max_backoff_seconds=self._max_backoff,
                    rng=self._rng,
                )
            )
        if outcome is MessageOutcome.retry:  # unreachable: attempt == max is never retryable
            raise FatalConsumerError("retry outcome after the attempt budget was exhausted")

        if outcome is MessageOutcome.dead_lettered:
            remaining = await asyncio.to_thread(self._dlq.flush, 10.0)
            if remaining:
                raise FatalConsumerError(f"{remaining} DLQ message(s) not delivered")
        await asyncio.to_thread(self._client.commit, message, False)
        KAFKA_MESSAGES.labels(self._topic, outcome.value).inc()
        KAFKA_HANDLER_DURATION.labels(self._topic).observe(time.perf_counter() - started)
        return outcome

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                self._heartbeat()
                await self.run_once()
        finally:
            await asyncio.to_thread(self._client.close)


def create_confluent_consumer(settings: KafkaSettings, *, group_id: str) -> Any:
    """Real client. Manual commits, earliest reset (a new group must not skip history).
    Reviewed, not executed against a broker here."""
    from confluent_kafka import Consumer

    return Consumer(
        {
            **base_client_config(settings),
            "group.id": group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
            "max.poll.interval.ms": 300_000,
            "partition.assignment.strategy": "cooperative-sticky",
        }
    )


class LagClient(Protocol):
    def assignment(self) -> list[Any]: ...
    def committed(self, partitions: list[Any], timeout: float = ...) -> list[Any]: ...
    def get_watermark_offsets(self, partition: Any, timeout: float = ...) -> tuple[int, int]: ...


def report_consumer_lag(client: LagClient, *, topic: str) -> dict[int, int]:
    """Sets `ekc_kafka_consumer_lag_messages{topic,partition}` = log end - committed offset
    for each assigned partition. A partition with no committed offset yet counts from the
    log start (low watermark). Call periodically (e.g. every 15s) from the runner's process.
    Reviewed against confluent_kafka's API, NOT run against a broker here."""
    lag: dict[int, int] = {}
    partitions = [p for p in client.assignment() if p.topic == topic]
    if not partitions:
        return lag
    for committed in client.committed(partitions, 10.0):
        low, high = client.get_watermark_offsets(committed, 10.0)
        position = committed.offset if committed.offset >= 0 else low
        lag[committed.partition] = max(0, high - position)
        KAFKA_CONSUMER_LAG.labels(topic, str(committed.partition)).set(lag[committed.partition])
    return lag
