"""
Consumer message-handling logic (Section 45-47, 50).

`handle_message` is the actual decision logic every Kafka consumer in this system runs
per message, deliberately factored out from any real confluent_kafka Consumer object so
it can be driven by tests with a raw bytes payload, a fake handler, and a real
IdempotencyGuard (backed by the real Redis in this environment) -- the only thing NOT
exercised here is an actual broker socket.

Decision flow per message:
  1. Parse the envelope. A parse failure is a poison message (Section 45) -- dead-letter
     immediately, no retry, since retrying can't fix malformed JSON.
  2. Idempotency claim (Section 46). Already claimed -> SKIPPED_DUPLICATE, no handler
     call, no side effects.
  3. Run the handler. Success -> PROCESSED.
  4. Handler raised. Non-retryable error type or attempt budget exhausted -> DLQ.
     Otherwise -> release the idempotency claim (so the retried delivery isn't itself
     treated as a duplicate) and return RETRY for the caller to requeue with backoff.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import ValidationError

from packages.events.schemas import EventEnvelope
from packages.kafka.idempotency import IdempotencyGuard
from packages.kafka.producer import EventProducer
from packages.kafka.retry import DLQEnvelope, is_retryable
from packages.kafka.topics import Topics
from packages.observability.logging import get_logger

logger = get_logger()


class MessageOutcome(StrEnum):
    processed = "processed"
    skipped_duplicate = "skipped_duplicate"
    retry = "retry"
    dead_lettered = "dead_lettered"


async def handle_message(
    raw_payload: bytes,
    *,
    main_topic: str,
    handler: Callable[[EventEnvelope], Awaitable[None]],
    idempotency_guard: IdempotencyGuard,
    dlq_producer: EventProducer,
    consumer_group: str,
    attempt_count: int = 1,
    max_attempts: int = 5,
) -> MessageOutcome:
    try:
        event = EventEnvelope.model_validate_json(raw_payload)
    except ValidationError as exc:
        logger.error("poison_message_received", error=str(exc)[:500], main_topic=main_topic)
        # A schema-invalid message has no EventEnvelope to wrap, so it goes to the DLQ topic
        # as a self-describing raw record: the original bytes (base64) plus why and who.
        dlq_producer.publish_raw(
            Topics.dlq_topic_for(main_topic),
            key=hashlib.sha256(raw_payload).hexdigest().encode("ascii"),
            value=json.dumps(
                {
                    "poison": True,
                    "error_type": "ValidationError",
                    "error_message": str(exc)[:1000],
                    "consumer_group": consumer_group,
                    "received_at": datetime.now(UTC).isoformat(),
                    "raw_payload_b64": base64.b64encode(raw_payload).decode("ascii"),
                }
            ).encode("utf-8"),
        )
        return MessageOutcome.dead_lettered

    dedup_key = event.dedup_key()
    claimed = await idempotency_guard.claim(dedup_key)
    if not claimed:
        logger.info("duplicate_event_skipped", event_id=str(event.event_id))
        return MessageOutcome.skipped_duplicate

    try:
        await handler(event)
        return MessageOutcome.processed
    except Exception as exc:  # noqa: BLE001 -- must classify every possible handler failure
        error_type = type(exc).__name__
        retryable = is_retryable(error_type) and attempt_count < max_attempts

        if retryable:
            # Release the claim: the NEXT delivery attempt (this is a real retry, not
            # a redelivery of an already-succeeded event) must be allowed to proceed.
            await idempotency_guard.release(dedup_key)
            logger.warning(
                "message_handler_failed_will_retry",
                event_id=str(event.event_id),
                error_type=error_type,
                attempt_count=attempt_count,
            )
            return MessageOutcome.retry

        dlq_envelope = DLQEnvelope.from_failed_event(
            event=event,
            error=exc,
            attempt_count=attempt_count,
            first_attempted_at=datetime.now(UTC),
            consumer_group=consumer_group,
        )
        dlq_producer.publish(
            Topics.dlq_topic_for(main_topic),
            EventEnvelope(
                event_type=event.event_type,
                tenant_id=event.tenant_id,
                producer=consumer_group,
                correlation_id=event.correlation_id,
                trace_id=event.trace_id,
                partition_key=event.partition_key,
                payload=dlq_envelope.to_kafka_payload(),
            ),
        )
        logger.error(
            "message_dead_lettered",
            event_id=str(event.event_id),
            error_type=error_type,
            attempt_count=attempt_count,
        )
        return MessageOutcome.dead_lettered
