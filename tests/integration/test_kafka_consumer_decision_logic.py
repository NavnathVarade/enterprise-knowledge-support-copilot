"""
Proves the full consumer decision logic (packages.kafka.consumer.handle_message)
against a real Redis-backed idempotency guard: processed / duplicate-skipped / retry /
dead-lettered / poison-message, exactly as Section 45-47 describe them.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from packages.config.settings import get_settings
from packages.domain.errors import InvalidDocument
from packages.events.schemas import EventEnvelope, EventType
from packages.kafka.consumer import MessageOutcome, handle_message
from packages.kafka.idempotency import IdempotencyGuard
from packages.kafka.producer import EventProducer
from packages.redis.client import get_redis_client


class _FakeDLQClient:
    def __init__(self) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: object = None) -> None:
        self.produced.append((topic, value, key))

    def flush(self, timeout: float = 10.0) -> int:
        return 0


@pytest.fixture
def guard() -> IdempotencyGuard:
    settings = get_settings()
    client = get_redis_client(settings.redis)
    return IdempotencyGuard(client, ttl_seconds=60)


def _make_event(payload: dict | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=uuid.uuid4(),
        producer="test",
        correlation_id=f"corr-{uuid.uuid4()}",
        trace_id=f"trace-{uuid.uuid4()}",
        partition_key=f"key-{uuid.uuid4()}",
        payload=payload or {"document_id": "doc-1"},
    )


@pytest.mark.integration
async def test_successful_processing_returns_processed(guard: IdempotencyGuard) -> None:
    event = _make_event()
    calls: list[EventEnvelope] = []

    async def handler(e: EventEnvelope) -> None:
        calls.append(e)

    outcome = await handle_message(
        event.model_dump_json().encode(),
        main_topic="document.ingested",
        handler=handler,
        idempotency_guard=guard,
        dlq_producer=EventProducer(_FakeDLQClient()),
        consumer_group="test-group",
    )

    assert outcome == MessageOutcome.processed
    assert len(calls) == 1
    assert calls[0].event_id == event.event_id


@pytest.mark.integration
async def test_redelivered_event_is_skipped_and_handler_not_called_again(
    guard: IdempotencyGuard,
) -> None:
    event = _make_event()
    call_count = 0

    async def handler(e: EventEnvelope) -> None:
        nonlocal call_count
        call_count += 1

    raw = event.model_dump_json().encode()
    dlq_producer = EventProducer(_FakeDLQClient())

    first_outcome = await handle_message(
        raw,
        main_topic="document.ingested",
        handler=handler,
        idempotency_guard=guard,
        dlq_producer=dlq_producer,
        consumer_group="test-group",
    )
    second_outcome = await handle_message(
        raw,
        main_topic="document.ingested",
        handler=handler,
        idempotency_guard=guard,
        dlq_producer=dlq_producer,
        consumer_group="test-group",
    )

    assert first_outcome == MessageOutcome.processed
    assert second_outcome == MessageOutcome.skipped_duplicate
    assert call_count == 1


@pytest.mark.integration
async def test_retryable_failure_releases_claim_for_next_attempt(guard: IdempotencyGuard) -> None:
    event = _make_event()

    async def failing_handler(e: EventEnvelope) -> None:
        raise ConnectionError("transient network blip")

    raw = event.model_dump_json().encode()
    outcome = await handle_message(
        raw,
        main_topic="document.ingested",
        handler=failing_handler,
        idempotency_guard=guard,
        dlq_producer=EventProducer(_FakeDLQClient()),
        consumer_group="test-group",
        attempt_count=1,
        max_attempts=5,
    )
    assert outcome == MessageOutcome.retry

    async def succeeding_handler(e: EventEnvelope) -> None:
        pass

    retry_outcome = await handle_message(
        raw,
        main_topic="document.ingested",
        handler=succeeding_handler,
        idempotency_guard=guard,
        dlq_producer=EventProducer(_FakeDLQClient()),
        consumer_group="test-group",
        attempt_count=2,
        max_attempts=5,
    )
    assert retry_outcome == MessageOutcome.processed


@pytest.mark.integration
async def test_non_retryable_failure_goes_straight_to_dlq(guard: IdempotencyGuard) -> None:
    event = _make_event()
    dlq_client = _FakeDLQClient()

    async def failing_handler(e: EventEnvelope) -> None:
        raise InvalidDocument("malformed content")

    outcome = await handle_message(
        event.model_dump_json().encode(),
        main_topic="document.ingested",
        handler=failing_handler,
        idempotency_guard=guard,
        dlq_producer=EventProducer(dlq_client),
        consumer_group="test-group",
        attempt_count=1,
        max_attempts=5,
    )

    assert outcome == MessageOutcome.dead_lettered
    assert len(dlq_client.produced) == 1
    topic, value, _key = dlq_client.produced[0]
    assert topic == "document.ingested.dlq"
    wrapper = EventEnvelope.model_validate_json(value)
    assert wrapper.payload["error_type"] == "InvalidDocument"
    assert wrapper.payload["attempt_count"] == 1


@pytest.mark.integration
async def test_exhausted_retries_go_to_dlq_even_for_retryable_errors(
    guard: IdempotencyGuard,
) -> None:
    event = _make_event()
    dlq_client = _FakeDLQClient()

    async def failing_handler(e: EventEnvelope) -> None:
        raise ConnectionError("still down")

    outcome = await handle_message(
        event.model_dump_json().encode(),
        main_topic="document.ingested",
        handler=failing_handler,
        idempotency_guard=guard,
        dlq_producer=EventProducer(dlq_client),
        consumer_group="test-group",
        attempt_count=5,
        max_attempts=5,
    )

    assert outcome == MessageOutcome.dead_lettered
    assert len(dlq_client.produced) == 1


@pytest.mark.integration
async def test_malformed_payload_is_dead_lettered_without_calling_handler(
    guard: IdempotencyGuard,
) -> None:
    calls = 0
    dlq_client = _FakeDLQClient()
    raw = b"this is not valid json at all {{{"

    async def handler(e: EventEnvelope) -> None:
        nonlocal calls
        calls += 1

    outcome = await handle_message(
        raw,
        main_topic="document.ingested",
        handler=handler,
        idempotency_guard=guard,
        dlq_producer=EventProducer(dlq_client),
        consumer_group="test-group",
    )

    assert outcome == MessageOutcome.dead_lettered
    assert calls == 0
    # The poison bytes are no longer lost: they reach the DLQ topic as a raw record.
    ((topic, value, _key),) = dlq_client.produced
    assert topic == "document.ingested.dlq"
    record = json.loads(value)
    assert record["poison"] is True
    assert base64.b64decode(record["raw_payload_b64"]) == raw
