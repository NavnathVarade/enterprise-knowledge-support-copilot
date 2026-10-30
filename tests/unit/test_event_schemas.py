from __future__ import annotations

import uuid

import pytest
from packages.events.schemas import EventEnvelope, EventType
from packages.kafka.topics import Topics, consumer_group_name, document_partition_key
from pydantic import ValidationError


@pytest.mark.unit
def test_event_envelope_generates_a_uuid_event_id_by_default() -> None:
    event = EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=uuid.uuid4(),
        producer="test",
        correlation_id="corr-1",
        trace_id="trace-1",
        partition_key="key-1",
        payload={"document_id": "abc"},
    )
    assert isinstance(event.event_id, uuid.UUID)


@pytest.mark.unit
def test_two_envelopes_get_different_event_ids() -> None:
    kwargs = {
        "event_type": EventType.document_ingested,
        "tenant_id": uuid.uuid4(),
        "producer": "test",
        "correlation_id": "corr-1",
        "trace_id": "trace-1",
        "partition_key": "key-1",
        "payload": {},
    }
    a = EventEnvelope(**kwargs)
    b = EventEnvelope(**kwargs)
    assert a.event_id != b.event_id


@pytest.mark.unit
def test_dedup_key_is_namespaced_by_event_type_and_id() -> None:
    event = EventEnvelope(
        event_type=EventType.document_ingested,
        tenant_id=uuid.uuid4(),
        producer="test",
        correlation_id="c",
        trace_id="t",
        partition_key="k",
        payload={},
    )
    key = event.dedup_key()
    assert "document.ingested" in key
    assert str(event.event_id) in key


@pytest.mark.unit
def test_envelope_round_trips_through_json() -> None:
    tenant_id = uuid.uuid4()
    original = EventEnvelope(
        event_type=EventType.document_processing_failed,
        tenant_id=tenant_id,
        producer="worker-1",
        correlation_id="corr-2",
        trace_id="trace-2",
        partition_key="k",
        payload={"error": "parse failure"},
    )
    raw = original.model_dump_json().encode("utf-8")
    restored = EventEnvelope.model_validate_json(raw)
    assert restored.event_id == original.event_id
    assert restored.tenant_id == tenant_id
    assert restored.payload == {"error": "parse failure"}


@pytest.mark.unit
def test_missing_required_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate_json(b'{"event_type": "document.ingested"}')


@pytest.mark.unit
def test_document_partition_key_combines_tenant_and_document() -> None:
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    key = document_partition_key(tenant_id=tenant_id, document_id=document_id)
    assert key == f"{tenant_id}:{document_id}"


@pytest.mark.unit
def test_retry_and_dlq_topic_names_are_derived_consistently() -> None:
    assert Topics.retry_topic_for(Topics.DOCUMENT_INGESTED) == "document.ingested.retry"
    assert Topics.dlq_topic_for(Topics.DOCUMENT_INGESTED) == "document.ingested.dlq"


@pytest.mark.unit
def test_consumer_group_name_includes_prefix() -> None:
    assert consumer_group_name(prefix="ekc", logical_name="parser") == "ekc-parser"
