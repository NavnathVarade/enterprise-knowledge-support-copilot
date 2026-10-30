"""
Event envelope (Section 44-46).

Every Kafka message published by this system is a JSON-serialized `EventEnvelope`.
`event_id` is what makes at-least-once delivery safe to consume idempotently (Section
46): a consumer that has already processed a given `event_id` must skip it on redelivery
rather than repeat its side effect. `correlation_id`/`trace_id` let a single user request
be followed across the API -> Kafka -> worker -> database boundary (Section 61).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """
    The event catalog (Section 44). Only types actually produced/consumed by code that
    exists get added here -- this is deliberately not the full Section 44 list, which
    includes events for phases (embedding, indexing, permission changes) not yet built.
    """

    document_ingested = "document.ingested"
    document_processing_completed = "document.processing.completed"
    document_processing_failed = "document.processing.failed"


class EventEnvelope(BaseModel):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: EventType
    schema_version: int = 1
    tenant_id: uuid.UUID
    event_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ingestion_time: datetime | None = None  # set by the consumer, not the producer
    producer: str
    correlation_id: str
    trace_id: str
    partition_key: str
    traceparent: str | None = None  # W3C trace context; optional (schema_version stays 1)
    payload: dict[str, Any]

    def dedup_key(self) -> str:
        """
        The Redis key used by the idempotency guard (packages/kafka/idempotency.py) to
        detect a redelivered event. Namespaced by event_type so two different event
        types can never collide even in the (practically impossible, since event_id is
        a UUID4) case of an ID collision.
        """
        return f"kafka:dedup:{self.event_type.value}:{self.event_id}"


class DocumentIngestedPayload(BaseModel):
    """Payload of `document.ingested`. Deliberately carries NO object key: the worker derives
    the key from (tenant, document, content_hash), so an event can never redirect a worker to
    another tenant's object."""

    document_id: uuid.UUID
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    mime_type: str
    filename: str
    size_bytes: int = Field(ge=1)


class DocumentProcessingCompletedPayload(BaseModel):
    document_id: uuid.UUID
    version: int
    chunk_count: int
    embedding_model: str


class DocumentProcessingFailedPayload(BaseModel):
    document_id: uuid.UUID
    error_type: str
    error_message: str
