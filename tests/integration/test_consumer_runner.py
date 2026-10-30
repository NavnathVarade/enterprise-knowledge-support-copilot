"""ConsumerRunner loop logic with a fake Kafka client and the real Redis guard."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
from packages.config.settings import get_settings
from packages.events.schemas import EventEnvelope, EventType
from packages.kafka.consumer import MessageOutcome
from packages.kafka.idempotency import IdempotencyGuard
from packages.kafka.producer import EventProducer
from packages.kafka.runner import ConsumerRunner, FatalConsumerError
from packages.redis.client import get_redis_client

pytestmark = pytest.mark.integration


class Msg:
    def __init__(self, value: bytes | None, error: Any = None) -> None:
        self._v, self._e = value, error

    def value(self) -> bytes | None:
        return self._v

    def error(self) -> Any:
        return self._e

    def topic(self) -> str:
        return "document.ingested"

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return 1


class FakeConsumer:
    def __init__(self, msgs: list[Msg]) -> None:
        self.msgs = msgs
        self.commits: list[Msg] = []
        self.subscribed: list[str] = []
        self.closed = False

    def subscribe(self, topics: list[str]) -> None:
        self.subscribed = topics

    def poll(self, timeout: float) -> Msg | None:
        return self.msgs.pop(0) if self.msgs else None

    def commit(self, message: Msg, asynchronous: bool = False) -> None:
        self.commits.append(message)

    def close(self) -> None:
        self.closed = True


class FakeProducerClient:
    def __init__(self, remaining: int = 0) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []
        self.remaining = remaining

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any = None) -> None:
        self.produced.append((topic, value, key))

    def flush(self, timeout: float = 10.0) -> int:
        return self.remaining


class Class(Exception):
    pass


def _event() -> EventEnvelope:
    return EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=uuid.uuid4(),
        producer="t",
        correlation_id="c",
        trace_id="t",
        partition_key="k",
        payload={"document_id": str(uuid.uuid4())},
    )


def _runner(consumer: FakeConsumer, handler: Any, dlq: FakeProducerClient | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    r = ConsumerRunner(
        consumer,
        topic="document.ingested",
        handler=handler,
        idempotency_guard=IdempotencyGuard(get_redis_client(get_settings().redis), ttl_seconds=60),
        dlq_producer=EventProducer(dlq or FakeProducerClient()),
        consumer_group="g",
        sleep=fake_sleep,
        **kw,
    )
    r.sleeps = sleeps  # type: ignore[attr-defined]
    return r


async def test_processed_message_is_committed_after_handling() -> None:
    seen: list[EventEnvelope] = []

    async def handler(e: EventEnvelope) -> None:
        assert not consumer.commits  # not committed before the handler finishes
        seen.append(e)

    ev = _event()
    msg = Msg(ev.model_dump_json().encode())
    consumer = FakeConsumer([msg])
    runner = _runner(consumer, handler)
    assert consumer.subscribed == ["document.ingested"]
    assert await runner.run_once() is MessageOutcome.processed
    assert seen[0].event_id == ev.event_id and consumer.commits == [msg]
    assert await runner.run_once() is None  # empty poll: nothing committed
    assert len(consumer.commits) == 1


async def test_transient_failures_back_off_then_succeed() -> None:
    calls = 0

    async def handler(e: EventEnvelope) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("down")

    consumer = FakeConsumer([Msg(_event().model_dump_json().encode())])
    runner = _runner(consumer, handler, backoff_base_seconds=1.0)
    assert await runner.run_once() is MessageOutcome.processed
    assert calls == 3 and len(runner.sleeps) == 2  # type: ignore[attr-defined]
    assert runner.sleeps[1] > runner.sleeps[0] * 1.3  # type: ignore[attr-defined]  # exponential
    assert len(consumer.commits) == 1


async def test_exhausted_retries_go_to_dlq_then_commit() -> None:
    async def handler(e: EventEnvelope) -> None:
        raise ConnectionError("still down")

    dlq = FakeProducerClient()
    consumer = FakeConsumer([Msg(_event().model_dump_json().encode())])
    runner = _runner(consumer, handler, dlq, max_attempts=3)
    assert await runner.run_once() is MessageOutcome.dead_lettered
    assert [t for t, _, _ in dlq.produced] == ["document.ingested.dlq"]
    assert len(consumer.commits) == 1
    assert len(runner.sleeps) == 2  # type: ignore[attr-defined]


async def test_permanent_error_is_dead_lettered_without_retrying() -> None:
    from packages.domain.errors import InvalidDocument

    async def handler(e: EventEnvelope) -> None:
        raise InvalidDocument("bad")

    dlq = FakeProducerClient()
    consumer = FakeConsumer([Msg(_event().model_dump_json().encode())])
    runner = _runner(consumer, handler, dlq)
    assert await runner.run_once() is MessageOutcome.dead_lettered
    assert runner.sleeps == [] and len(dlq.produced) == 1  # type: ignore[attr-defined]


async def test_poison_message_reaches_dlq_raw_and_is_committed() -> None:
    async def handler(e: EventEnvelope) -> None:
        raise AssertionError("must not run")

    dlq = FakeProducerClient()
    consumer = FakeConsumer([Msg(b"{{garbage")])
    runner = _runner(consumer, handler, dlq)
    assert await runner.run_once() is MessageOutcome.dead_lettered
    record = json.loads(dlq.produced[0][1])
    assert base64.b64decode(record["raw_payload_b64"]) == b"{{garbage"
    assert len(consumer.commits) == 1


async def test_undelivered_dlq_record_prevents_the_commit() -> None:
    async def handler(e: EventEnvelope) -> None:
        raise ValueError("permanent")

    consumer = FakeConsumer([Msg(_event().model_dump_json().encode())])
    runner = _runner(consumer, handler, FakeProducerClient(remaining=1))
    with pytest.raises(FatalConsumerError):
        await runner.run_once()
    assert consumer.commits == []


async def test_fatal_broker_error_stops_the_runner_and_transient_ones_do_not() -> None:
    class Err:
        def __init__(self, fatal: bool) -> None:
            self._f = fatal

        def fatal(self) -> bool:
            return self._f

    async def handler(e: EventEnvelope) -> None:
        return None

    consumer = FakeConsumer([Msg(None, Err(False)), Msg(None, Err(True))])
    runner = _runner(consumer, handler)
    assert await runner.run_once() is None  # transient: logged, skipped, not committed
    with pytest.raises(FatalConsumerError):
        await runner.run_once()
    assert consumer.commits == []


async def test_run_forever_stops_on_event_and_closes_the_client() -> None:
    import asyncio

    async def handler(e: EventEnvelope) -> None:
        return None

    consumer = FakeConsumer([Msg(_event().model_dump_json().encode())])
    runner = _runner(consumer, handler)
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert consumer.closed and len(consumer.commits) == 1
