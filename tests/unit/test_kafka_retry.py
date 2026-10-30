from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime

import pytest
from packages.events.schemas import EventEnvelope, EventType
from packages.kafka.retry import (
    NON_RETRYABLE_ERROR_TYPES,
    DLQEnvelope,
    compute_backoff_seconds,
    is_retryable,
)


@pytest.mark.unit
def test_backoff_grows_exponentially_before_the_cap() -> None:
    rng = random.Random(42)
    b1 = compute_backoff_seconds(
        1, base_seconds=1.0, max_backoff_seconds=1000, jitter_fraction=0, rng=rng
    )
    b2 = compute_backoff_seconds(
        2, base_seconds=1.0, max_backoff_seconds=1000, jitter_fraction=0, rng=rng
    )
    b3 = compute_backoff_seconds(
        3, base_seconds=1.0, max_backoff_seconds=1000, jitter_fraction=0, rng=rng
    )
    assert b1 == pytest.approx(1.0)
    assert b2 == pytest.approx(2.0)
    assert b3 == pytest.approx(4.0)


@pytest.mark.unit
def test_backoff_is_capped_at_max() -> None:
    rng = random.Random(1)
    backoff = compute_backoff_seconds(
        20, base_seconds=1.0, max_backoff_seconds=30.0, jitter_fraction=0, rng=rng
    )
    assert backoff == pytest.approx(30.0)


@pytest.mark.unit
def test_backoff_jitter_stays_within_bounds() -> None:
    rng = random.Random(7)
    for attempt in range(1, 10):
        backoff = compute_backoff_seconds(
            attempt, base_seconds=1.0, max_backoff_seconds=60.0, jitter_fraction=0.2, rng=rng
        )
        capped = min(1.0 * (2 ** (attempt - 1)), 60.0)
        assert capped * 0.8 <= backoff <= capped * 1.2 + 1e-9


@pytest.mark.unit
def test_backoff_never_negative() -> None:
    rng = random.Random(0)
    for attempt in range(1, 15):
        assert compute_backoff_seconds(attempt, rng=rng) >= 0.0


@pytest.mark.unit
def test_backoff_rejects_attempt_below_one() -> None:
    with pytest.raises(ValueError):
        compute_backoff_seconds(0)


@pytest.mark.unit
def test_known_non_retryable_errors_are_rejected() -> None:
    for error_type in NON_RETRYABLE_ERROR_TYPES:
        assert not is_retryable(error_type)


@pytest.mark.unit
def test_unknown_errors_are_retryable_by_default() -> None:
    assert is_retryable("ConnectionResetError")
    assert is_retryable("TimeoutError")


@pytest.mark.unit
def test_dlq_envelope_from_failed_event_captures_error_details() -> None:
    event = EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=uuid.uuid4(),
        producer="worker",
        correlation_id="c1",
        trace_id="t1",
        partition_key="k1",
        payload={"document_id": "doc-1"},
    )
    error = ValueError("something went wrong")
    dlq = DLQEnvelope.from_failed_event(
        event=event,
        error=error,
        attempt_count=5,
        first_attempted_at=datetime.now(UTC),
        consumer_group="ekc-parser",
    )
    assert dlq.error_type == "ValueError"
    assert dlq.error_message == "something went wrong"
    assert dlq.attempt_count == 5
    assert dlq.original_event.event_id == event.event_id
    assert dlq.correlation_id == event.correlation_id


@pytest.mark.unit
def test_dlq_envelope_serializes_to_json_safe_payload() -> None:
    event = EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=uuid.uuid4(),
        producer="worker",
        correlation_id="c1",
        trace_id="t1",
        partition_key="k1",
        payload={},
    )
    dlq = DLQEnvelope.from_failed_event(
        event=event,
        error=RuntimeError("boom"),
        attempt_count=1,
        first_attempted_at=datetime.now(UTC),
        consumer_group="ekc-parser",
    )
    payload = dlq.to_kafka_payload()
    assert payload["error_type"] == "RuntimeError"
    assert isinstance(payload["original_event"], dict)
