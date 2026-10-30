"""
Retry backoff and dead-letter-queue envelope (Section 47, 50, 141).

`compute_backoff_seconds` is pure and deterministic given a seeded RNG, so it's fully
unit-testable: bounded exponential growth, capped at `max_backoff_seconds`, with jitter
to avoid a thundering-herd retry storm across many consumers failing at once.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from packages.events.schemas import EventEnvelope


def compute_backoff_seconds(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    max_backoff_seconds: float = 60.0,
    jitter_fraction: float = 0.2,
    rng: random.Random | None = None,
) -> float:
    """
    attempt=1 -> ~base_seconds, attempt=2 -> ~2*base_seconds, ... capped at
    max_backoff_seconds, with up to +/- jitter_fraction of randomness so many
    consumers retrying the same failure don't all retry in lockstep.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    # Retry jitter, not a security primitive.
    rng = rng or random.Random()  # nosec B311

    uncapped = base_seconds * (2 ** (attempt - 1))
    capped = min(uncapped, max_backoff_seconds)

    jitter_range = capped * jitter_fraction
    jitter = rng.uniform(-jitter_range, jitter_range)
    return max(0.0, float(capped + jitter))


# Errors we know are never worth retrying -- a malformed schema or an authorization
# failure will fail identically on the 2nd, 10th, and 100th attempt (Section 50).
NON_RETRYABLE_ERROR_TYPES: frozenset[str] = frozenset(
    {
        "ValidationError",
        "UnauthorizedDocumentAccess",
        "InvalidDocument",
        "IllegalStatusTransition",
        "DocumentNotFound",
        "ObjectNotFound",
        "UnsupportedMimeType",
        "ParsingFailed",
        "PayloadTooLarge",
    }
)


def is_retryable(error_type_name: str) -> bool:
    return error_type_name not in NON_RETRYABLE_ERROR_TYPES


class DLQEnvelope(BaseModel):
    dlq_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    original_event: EventEnvelope
    error_type: str
    error_message: str
    attempt_count: int
    first_attempted_at: datetime
    last_attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consumer_group: str
    correlation_id: str

    @classmethod
    def from_failed_event(
        cls,
        *,
        event: EventEnvelope,
        error: Exception,
        attempt_count: int,
        first_attempted_at: datetime,
        consumer_group: str,
    ) -> DLQEnvelope:
        return cls(
            original_event=event,
            error_type=type(error).__name__,
            error_message=str(error),
            attempt_count=attempt_count,
            first_attempted_at=first_attempted_at,
            consumer_group=consumer_group,
            correlation_id=event.correlation_id,
        )

    def to_kafka_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
