from __future__ import annotations

import uuid

import pytest
from packages.events.schemas import EventEnvelope, EventType
from packages.kafka.producer import EventProducer


class _FakeKafkaClient:
    def __init__(self) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []
        self.flush_calls: list[float] = []

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: object = None) -> None:
        self.produced.append((topic, value, key))

    def flush(self, timeout: float = 10.0) -> int:
        self.flush_calls.append(timeout)
        return 0


@pytest.mark.unit
def test_publish_sends_serialized_envelope_with_partition_key() -> None:
    fake_client = _FakeKafkaClient()
    producer = EventProducer(fake_client)
    event = EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=uuid.uuid4(),
        producer="test",
        correlation_id="c1",
        trace_id="t1",
        partition_key="tenant-1:doc-1",
        payload={"document_id": "doc-1"},
    )

    producer.publish("document.ingested", event)

    assert len(fake_client.produced) == 1
    topic, value, key = fake_client.produced[0]
    assert topic == "document.ingested"
    assert key == b"tenant-1:doc-1"

    restored = EventEnvelope.model_validate_json(value)
    assert restored.event_id == event.event_id


@pytest.mark.unit
def test_flush_delegates_to_client() -> None:
    fake_client = _FakeKafkaClient()
    producer = EventProducer(fake_client)
    producer.flush(timeout_seconds=5.0)
    assert fake_client.flush_calls == [5.0]
