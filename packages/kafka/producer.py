"""
Kafka producer.

`EventProducer` doesn't import confluent_kafka's `Producer` directly into its
constructor's type -- it depends on `KafkaProducerClient`, a small Protocol covering
just the two calls it needs (`produce`, `flush`). `create_confluent_producer` is the
only place that actually constructs a real `confluent_kafka.Producer`, with the
idempotent-producer settings Section 45 requires (`enable.idempotence=true`,
`acks=all`). This split is what makes `EventProducer.publish`'s serialization/key
logic unit-testable with a fake client, independent of whether a real broker is
reachable -- this environment has no live Kafka broker to test the real client
against; that gap is stated explicitly rather than glossed over (see
docs/kafka/PHASE_8_KAFKA_DESIGN.md).
"""

from __future__ import annotations

from typing import Any, Protocol

from packages.config.settings import KafkaSettings
from packages.events.schemas import EventEnvelope
from packages.kafka.client_config import base_client_config


class KafkaProducerClient(Protocol):
    def produce(
        self,
        topic: str,
        value: bytes,
        key: bytes,
        on_delivery: Any = None,
    ) -> None: ...

    def flush(self, timeout: float = ...) -> int: ...


class EventProducer:
    def __init__(self, client: KafkaProducerClient) -> None:
        self._client = client

    def publish(self, topic: str, event: EventEnvelope, *, on_delivery: Any = None) -> None:
        """`on_delivery(err, msg)` is confluent_kafka's delivery-report callback: it is how a
        caller learns whether the broker actually acknowledged the message (produce() alone
        only enqueues it)."""
        payload = event.model_dump_json().encode("utf-8")
        key = event.partition_key.encode("utf-8")
        if on_delivery is None:
            self._client.produce(topic, value=payload, key=key)
        else:
            self._client.produce(topic, value=payload, key=key, on_delivery=on_delivery)

    def publish_raw(self, topic: str, *, key: bytes, value: bytes) -> None:
        """For payloads that are not valid EventEnvelopes (poison-message DLQ records)."""
        self._client.produce(topic, value=value, key=key)

    def flush(self, timeout_seconds: float = 10.0) -> int:
        """Returns the number of messages still in the outbound queue after timeout."""
        return self._client.flush(timeout_seconds)


def create_confluent_producer(settings: KafkaSettings) -> Any:
    """
    Constructs a real confluent_kafka.Producer. Not covered by unit tests (would
    require a live broker); imported lazily so this module remains importable and
    testable even without the confluent_kafka runtime fully configured for a live
    connection.
    """
    from confluent_kafka import Producer

    return Producer(
        {
            **base_client_config(settings),
            "enable.idempotence": settings.producer_enable_idempotence,
            "acks": settings.producer_acks,
        }
    )
