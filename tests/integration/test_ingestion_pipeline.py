"""
Phase 28: upload -> outbox -> relay -> consumer -> pipeline -> searchable.

Real Postgres (documents, versions, chunks, embeddings, outbox, FOR UPDATE SKIP LOCKED),
real Redis idempotency guard, real parsers/chunker, deterministic embeddings, in-memory
object store, and a fake Kafka client. NOT covered: a real broker or real S3 (S3 code is
covered separately against moto).
"""

from __future__ import annotations

import io
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any

import pytest
from apps.api.app.main import create_app
from httpx import ASGITransport, AsyncClient
from packages.auth.identity import Identity
from packages.config.settings import get_settings
from packages.database.models import Document, Tenant, User
from packages.database.models.documents import DocumentChunk, DocumentVersion, Embedding
from packages.database.models.outbox import OutboxEvent
from packages.database.session import get_sessionmaker
from packages.domain.errors import DocumentNotFound, InvalidDocument, ParsingFailed
from packages.events.schemas import EventEnvelope
from packages.ingestion.pipeline import IngestionDependencies, handle_document_ingested
from packages.kafka.consumer import MessageOutcome, handle_message
from packages.kafka.idempotency import IdempotencyGuard
from packages.kafka.producer import EventProducer
from packages.kafka.topics import Topics
from packages.redis.client import get_redis_client
from packages.security.passwords import hash_password
from packages.storage.object_store import (
    InMemoryObjectStore,
    ObjectNotFound,
    ObjectStoreError,
)
from packages.workers.outbox_relay import OutboxRelay
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.retrieval.hybrid import hybrid_search_authorized
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration
EMB = DeterministicLocalEmbeddingProvider(dimensions=1024)
MD = b"# Refund Policy\n\n## Eligibility\n\nCustomers may request a refund within 30 days of purchase."


class FakeKafka:
    """mode: ack | fail | silent. Records produced messages; flush() fires callbacks."""

    def __init__(self, mode: str = "ack") -> None:
        self.mode = mode
        self.produced: list[tuple[str, bytes, bytes]] = []
        self._pending: list[Any] = []

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any = None) -> None:
        self.produced.append((topic, value, key))
        if on_delivery is not None:
            self._pending.append(on_delivery)

    def flush(self, timeout: float = 10.0) -> int:
        for cb in self._pending:
            if self.mode == "ack":
                cb(None, object())
            elif self.mode == "fail":
                cb("broker down", object())
        self._pending.clear()
        return 0


class FlakyStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = 0

    async def get(self, key: str, *, max_bytes: int) -> bytes:
        if self.fail_next:
            self.fail_next -= 1
            raise ObjectStoreError()
        return await super().get(key, max_bytes=max_bytes)


class ExplodingEmbedder(DeterministicLocalEmbeddingProvider):
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding provider exploded")


@dataclass
class Uploaded:
    tenant: uuid.UUID
    user: uuid.UUID
    document_id: uuid.UUID
    content_hash: str


@pytest.fixture
async def db() -> AsyncSession:
    async with get_sessionmaker(get_settings().database)() as session:
        yield session
        await session.rollback()


@pytest.fixture
def sessionmaker() -> Any:
    return get_sessionmaker(get_settings().database)


@pytest.fixture
def guard() -> IdempotencyGuard:
    return IdempotencyGuard(get_redis_client(get_settings().redis), ttl_seconds=60)


async def _upload(
    db: AsyncSession,
    store: InMemoryObjectStore,
    content: bytes = MD,
    name: str = "policy.md",
    mime: str = "text/markdown",
    tenant: Tenant | None = None,
) -> Uploaded:
    password = "pw-ingest-1"
    if tenant is None:
        tenant = Tenant(name="Ing", slug=f"ing-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.flush()
    tenant_id, tenant_slug = tenant.id, tenant.slug
    user = User(
        tenant_id=tenant_id,
        email=f"u{uuid.uuid4().hex[:6]}@example.com",
        hashed_password=hash_password(password),
        display_name="U",
    )
    db.add(user)
    await db.flush()
    user_id, user_email = user.id, user.email
    await db.commit()
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.object_store = store
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            login = await c.post(
                "/api/v1/auth/login",
                json={"tenant_slug": tenant_slug, "email": user_email, "password": password},
            )
            token = login.json()["access_token"]
            resp = await c.post(
                "/api/v1/documents",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (name, content, mime)},
            )
    assert resp.status_code == 202, f"{resp.status_code} {resp.text}"
    body = resp.json()
    return Uploaded(tenant_id, user_id, uuid.UUID(body["id"]), body["content_hash"])


def _deps(sm: Any, store: InMemoryObjectStore, embedder: Any = EMB) -> IngestionDependencies:
    return IngestionDependencies(
        sessionmaker=sm,
        store=store,
        embedding_provider=embedder,
        ingestion_settings=get_settings().ingestion,
    )


async def _outbox(db: AsyncSession, tenant: uuid.UUID, topic: str) -> list[OutboxEvent]:
    db.expire_all()
    rows = await db.execute(
        select(OutboxEvent).where(OutboxEvent.tenant_id == tenant, OutboxEvent.topic == topic)
    )
    return list(rows.scalars().all())


async def _doc(db: AsyncSession, up: Uploaded) -> Document:
    db.expire_all()
    return (await db.execute(select(Document).where(Document.id == up.document_id))).scalar_one()


async def _ingested_event(db: AsyncSession, up: Uploaded) -> EventEnvelope:
    (row,) = await _outbox(db, up.tenant, Topics.DOCUMENT_INGESTED)
    return EventEnvelope.model_validate(row.envelope)


async def _count(db: AsyncSession, model: Any, up: Uploaded) -> int:
    col = model.document_id if model is DocumentVersion else model.tenant_id
    value = up.document_id if model is DocumentVersion else up.tenant
    return int(
        (await db.execute(select(func.count()).select_from(model).where(col == value))).scalar_one()
    )


# ------------------------------------------------------------------ upload + outbox


async def test_upload_stores_original_and_writes_outbox_event_atomically(db: AsyncSession) -> None:
    store = InMemoryObjectStore()
    up = await _upload(db, store)
    doc = await _doc(db, up)
    assert doc.status == "uploaded" and doc.source_uri and doc.source_uri.startswith("memory://")
    assert len(store.objects) == 1
    ((key, (data, ctype)),) = store.objects.items()
    assert key.endswith(f"/{up.content_hash}") and str(up.tenant) in key and data == MD
    assert ctype == "text/markdown"
    ev = await _ingested_event(db, up)
    assert ev.payload["document_id"] == str(up.document_id)
    assert ev.payload["content_hash"] == up.content_hash
    assert "key" not in ev.payload and "object_key" not in ev.payload


async def test_failed_object_put_commits_nothing(db: AsyncSession) -> None:
    class BrokenStore(InMemoryObjectStore):
        async def put(self, key: str, data: bytes, *, content_type: str) -> None:
            raise ObjectStoreError()

    tenant = Tenant(name="Brk", slug=f"brk-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    tenant_id = tenant.id
    with pytest.raises(AssertionError, match="503"):  # the app maps the store error to HTTP 503
        await _upload(db, BrokenStore(), tenant=tenant)
    db.expire_all()
    docs = await db.execute(select(Document).where(Document.tenant_id == tenant_id))
    assert docs.first() is None
    assert await _outbox(db, tenant_id, Topics.DOCUMENT_INGESTED) == []


# ------------------------------------------------------------------ relay


async def test_relay_publishes_only_acknowledged_rows(db: AsyncSession, sessionmaker: Any) -> None:
    up = await _upload(db, InMemoryObjectStore())
    kafka = FakeKafka("ack")
    relay = OutboxRelay(sessionmaker, EventProducer(kafka))
    result = await relay.run_once()
    assert result.published >= 1 and result.failed == 0
    (row,) = await _outbox(db, up.tenant, Topics.DOCUMENT_INGESTED)
    assert row.published_at is not None and row.attempts == 1
    topics = [t for t, _, _ in kafka.produced]
    assert Topics.DOCUMENT_INGESTED in topics
    # a second pass has nothing left for this tenant
    before = len(kafka.produced)
    await relay.run_once()
    assert not any(
        EventEnvelope.model_validate_json(v).tenant_id == up.tenant
        for _, v, _ in kafka.produced[before:]
    )


@pytest.mark.parametrize("mode", ["fail", "silent"])
async def test_relay_keeps_unacknowledged_rows_for_retry(
    db: AsyncSession, sessionmaker: Any, mode: str
) -> None:
    up = await _upload(db, InMemoryObjectStore())
    await OutboxRelay(sessionmaker, EventProducer(FakeKafka(mode))).run_once()
    (row,) = await _outbox(db, up.tenant, Topics.DOCUMENT_INGESTED)
    assert row.published_at is None and row.attempts >= 1 and row.last_error
    # broker recovers -> the same row goes out
    await OutboxRelay(sessionmaker, EventProducer(FakeKafka("ack"))).run_once()
    (row,) = await _outbox(db, up.tenant, Topics.DOCUMENT_INGESTED)
    assert row.published_at is not None


async def test_relay_blocks_later_events_of_the_same_key_after_a_failure(
    db: AsyncSession, sessionmaker: Any
) -> None:
    up = await _upload(db, InMemoryObjectStore())

    class FailFirst(FakeKafka):
        def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any = None) -> None:
            if EventEnvelope.model_validate_json(value).tenant_id == up.tenant:
                raise BufferError("queue full")
            super().produce(topic, value, key, on_delivery)

    # a second event for the same partition key, queued after the first
    async with sessionmaker() as s:
        first = await _ingested_event(db, up)
        second = first.model_copy(update={"event_id": uuid.uuid4()})
        from packages.database.repositories.outbox import enqueue_event

        await enqueue_event(s, topic=Topics.DOCUMENT_INGESTED, event=second)
        await s.commit()
    result = await OutboxRelay(sessionmaker, EventProducer(FailFirst("ack"))).run_once()
    assert result.failed >= 1 and result.skipped_for_ordering >= 1
    rows = await _outbox(db, up.tenant, Topics.DOCUMENT_INGESTED)
    assert all(r.published_at is None for r in rows)


async def test_concurrent_relays_never_claim_the_same_row(
    db: AsyncSession, sessionmaker: Any
) -> None:
    up = await _upload(db, InMemoryObjectStore())
    from packages.database.repositories.outbox import claim_unpublished

    async with sessionmaker() as s1, sessionmaker() as s2:
        first = {r.id for r in await claim_unpublished(s1, limit=10_000)}
        second = {r.id for r in await claim_unpublished(s2, limit=10_000)}
        await s1.rollback()
        await s2.rollback()
    (row,) = await _outbox(db, up.tenant, Topics.DOCUMENT_INGESTED)
    assert row.id in first and row.id not in second and not (first & second)


# ------------------------------------------------------------------ pipeline


async def _deliver(
    db: AsyncSession, up: Uploaded, deps: IngestionDependencies, guard: IdempotencyGuard
) -> MessageOutcome:
    event = await _ingested_event(db, up)
    return await handle_message(
        event.model_dump_json().encode(),
        main_topic=Topics.DOCUMENT_INGESTED,
        handler=lambda e: handle_document_ingested(deps, e),
        idempotency_guard=guard,
        dlq_producer=EventProducer(FakeKafka()),
        consumer_group="test-ingestion",
    )


async def test_end_to_end_document_becomes_active_and_searchable(
    db: AsyncSession, sessionmaker: Any, guard: IdempotencyGuard
) -> None:
    store = InMemoryObjectStore()
    up = await _upload(db, store)
    await OutboxRelay(sessionmaker, EventProducer(FakeKafka())).run_once()
    outcome = await _deliver(db, up, _deps(sessionmaker, store), guard)
    assert outcome is MessageOutcome.processed

    assert (await _doc(db, up)).status == "active"
    version = (
        await db.execute(
            select(DocumentVersion).where(DocumentVersion.document_id == up.document_id)
        )
    ).scalar_one()
    assert version.status == "published" and version.version == 1
    chunks = (
        (
            await db.execute(
                select(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
            )
        )
        .scalars()
        .all()
    )
    assert chunks and "30 days" in chunks[0].text
    emb = (
        await db.execute(
            select(func.count()).select_from(Embedding).where(Embedding.tenant_id == up.tenant)
        )
    ).scalar_one()
    assert emb == len(chunks)

    done = await _outbox(db, up.tenant, Topics.DOCUMENT_PROCESSING_COMPLETED)
    assert len(done) == 1 and done[0].envelope["payload"]["chunk_count"] == len(chunks)
    assert done[0].envelope["correlation_id"]  # trace carried through from the upload

    identity = Identity(up.user, up.tenant, "u@example.com", frozenset(), frozenset())
    hits = await hybrid_search_authorized(
        db,
        tenant_id=up.tenant,
        identity=identity,
        query="refund within 30 days",
        embedding_provider=EMB,
        limit=5,
    )
    assert hits and "30 days" in hits[0][0].text
    stranger = Identity(uuid.uuid4(), up.tenant, "x@example.com", frozenset(), frozenset())
    assert (
        await hybrid_search_authorized(
            db,
            tenant_id=up.tenant,
            identity=stranger,
            query="refund within 30 days",
            embedding_provider=EMB,
            limit=5,
        )
        == []
    )


async def test_redelivery_and_replay_do_not_duplicate_content(
    db: AsyncSession, sessionmaker: Any, guard: IdempotencyGuard
) -> None:
    store = InMemoryObjectStore()
    up = await _upload(db, store)
    deps = _deps(sessionmaker, store)
    assert await _deliver(db, up, deps, guard) is MessageOutcome.processed
    assert await _deliver(db, up, deps, guard) is MessageOutcome.skipped_duplicate
    # a NEW event for already-indexed content (guard doesn't know it) is a no-op too
    event = (await _ingested_event(db, up)).model_copy(update={"event_id": uuid.uuid4()})
    await handle_document_ingested(deps, event)
    assert await _count(db, DocumentVersion, up) == 1
    assert len(await _outbox(db, up.tenant, Topics.DOCUMENT_PROCESSING_COMPLETED)) == 1


async def test_transient_storage_failure_is_retryable_then_recovers(
    db: AsyncSession, sessionmaker: Any
) -> None:
    store = FlakyStore()
    up = await _upload(db, store)
    deps = _deps(sessionmaker, store)
    event = await _ingested_event(db, up)
    store.fail_next = 1
    with pytest.raises(ObjectStoreError):
        await handle_document_ingested(deps, event)
    assert (await _doc(db, up)).status == "processing_failed"
    assert await _outbox(db, up.tenant, Topics.DOCUMENT_PROCESSING_FAILED) == []  # retryable
    await handle_document_ingested(deps, event)  # processing_failed -> validating -> ... -> active
    assert (await _doc(db, up)).status == "active"


@pytest.mark.parametrize(
    ("scenario", "error", "status"),
    [
        ("missing_object", ObjectNotFound, "processing_failed"),
        ("tampered_object", InvalidDocument, "quarantined"),
        ("empty_text", InvalidDocument, "processing_failed"),
        ("corrupt_docx", ParsingFailed, "processing_failed"),
    ],
)
async def test_permanent_failures_are_recorded_and_announced(
    db: AsyncSession, sessionmaker: Any, scenario: str, error: type[Exception], status: str
) -> None:
    store = InMemoryObjectStore()
    if scenario == "empty_text":
        up = await _upload(db, store, content=b" \n \n ", name="blank.txt", mime="text/plain")
    elif scenario == "corrupt_docx":
        # Structurally a Word archive (passes upload validation) but the XML is junk, so the
        # failure happens in the parser -- the path this scenario exists to cover.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", "this is not xml")
        up = await _upload(
            db,
            store,
            content=buf.getvalue(),
            name="x.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    else:
        up = await _upload(db, store)
    if scenario == "missing_object":
        store.objects.clear()
    if scenario == "tampered_object":
        for k in list(store.objects):
            store.objects[k] = (b"# Something else entirely", "text/markdown")
    event = await _ingested_event(db, up)
    with pytest.raises(error):
        await handle_document_ingested(_deps(sessionmaker, store), event)
    assert (await _doc(db, up)).status == status
    failed = await _outbox(db, up.tenant, Topics.DOCUMENT_PROCESSING_FAILED)
    assert len(failed) == 1 and failed[0].envelope["payload"]["error_type"] == error.__name__
    assert await _count(db, DocumentVersion, up) == 0


async def test_failure_after_compute_leaves_no_partial_state(
    db: AsyncSession, sessionmaker: Any
) -> None:
    store = InMemoryObjectStore()
    up = await _upload(db, store)
    event = await _ingested_event(db, up)
    with pytest.raises(RuntimeError):
        await handle_document_ingested(
            _deps(sessionmaker, store, ExplodingEmbedder(dimensions=1024)), event
        )
    assert await _count(db, DocumentVersion, up) == 0
    chunks = (
        await db.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.tenant_id == up.tenant)
        )
    ).scalar_one()
    assert chunks == 0
    assert (await _doc(db, up)).status == "processing_failed"


async def test_event_cannot_cross_tenants_or_forge_hashes(
    db: AsyncSession, sessionmaker: Any
) -> None:
    store = InMemoryObjectStore()
    a = await _upload(db, store)
    other = Tenant(name="Other", slug=f"oth-{uuid.uuid4().hex[:8]}")
    db.add(other)
    await db.flush()
    other_id = other.id
    await db.commit()
    event = await _ingested_event(db, a)
    with pytest.raises(DocumentNotFound):  # tenant B's event naming tenant A's document
        await handle_document_ingested(
            _deps(sessionmaker, store), event.model_copy(update={"tenant_id": other_id})
        )
    forged = event.model_copy(update={"payload": {**event.payload, "content_hash": "0" * 64}})
    with pytest.raises(InvalidDocument):
        await handle_document_ingested(_deps(sessionmaker, store), forged)
    assert await _count(db, DocumentVersion, a) == 0
