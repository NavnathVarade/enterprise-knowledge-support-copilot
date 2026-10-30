"""
Document ingestion pipeline (Spec §48-52, §83): `document.ingested` -> fetch original ->
verify -> parse -> chunk -> embed -> index -> `document.processing.completed`.

Design rules
- The object key is DERIVED from (tenant, document, content_hash), never read from the
  event; the stored bytes are re-hashed and must equal the document's recorded hash.
- Three stages. (1) claim: short transaction moving the document to `validating`.
  (2) compute: fetch, verify, parse, chunk, embed -- NO database transaction is held open
  while talking to storage or the embedding provider. (3) commit: ONE transaction creates
  the version, chunks and embeddings, publishes the version, walks the lifecycle to
  `active`, and writes the completion event to the outbox. A crash or failure before (3)
  commits leaves no partial chunks and no half-published version.
- Idempotent: a redelivery for content already indexed is a no-op; the document row is
  locked during (3) so concurrent workers cannot both publish a version.
- Failures: the document is moved to `processing_failed` (retryable via the lifecycle) and,
  only for NON-retryable errors, a `document.processing.failed` event is written to the
  outbox. Retryable errors are re-raised for the consumer's backoff/DLQ logic.
- Because stage (3) is atomic, the `embedding_failed`/`indexing_failed` lifecycle states are
  not used by this pipeline; every failure maps to `processing_failed`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from enum import StrEnum

from rag.chunking.base import Chunk
from rag.chunking.chunker import chunk_document
from rag.embeddings.base import EmbeddingProvider, MeteredEmbeddingProvider
from rag.ingestion.validation import validate_extracted_text, validate_upload
from rag.parsing.base import ParsedDocument
from rag.parsing.registry import get_parser_for_mime_type
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.config.settings import IngestionSettings
from packages.database.models.documents import (
    Document,
    DocumentVersion,
    DocumentVersionStatus,
)
from packages.database.repositories.chunks import create_chunks_for_version
from packages.database.repositories.documents import create_draft_version, publish_version
from packages.database.repositories.embeddings import create_embeddings_for_chunks
from packages.database.repositories.outbox import enqueue_event
from packages.domain.document_lifecycle import assert_valid_transition
from packages.domain.document_status import DocumentStatus
from packages.domain.errors import DocumentNotFound, DomainError, InvalidDocument, ParsingFailed
from packages.domain.hashing import sha256_hex
from packages.events.schemas import (
    DocumentIngestedPayload,
    DocumentProcessingCompletedPayload,
    DocumentProcessingFailedPayload,
    EventEnvelope,
    EventType,
)
from packages.kafka.retry import is_retryable
from packages.kafka.topics import Topics, document_partition_key
from packages.observability.logging import get_logger
from packages.observability.metrics import (
    EMBEDDING_TOKENS,
    INGESTION_DOCUMENTS,
    INGESTION_STAGE,
    timed,
)
from packages.observability.tracing import context_from_traceparent, current_traceparent, span
from packages.storage.object_store import ObjectStore, document_object_key
from packages.usage.context import usage_context
from packages.usage.recorder import UsageRecord, UsageRecorder

_log = get_logger(component="ingestion_pipeline")

_FAILED_STATES = {
    DocumentStatus.processing_failed.value,
    DocumentStatus.embedding_failed.value,
    DocumentStatus.indexing_failed.value,
}
_TERMINAL_STATES = {
    DocumentStatus.quarantined.value,
    DocumentStatus.deleted.value,
    DocumentStatus.archived.value,
}
_EMBEDDING_VERSION = "v1"


class IngestionOutcome(StrEnum):
    COMPLETED = "completed"
    ALREADY_INDEXED = "already_indexed"
    SKIPPED_TERMINAL = "skipped_terminal"


@dataclass(frozen=True)
class IngestionDependencies:
    sessionmaker: async_sessionmaker[AsyncSession]
    store: ObjectStore
    embedding_provider: EmbeddingProvider
    ingestion_settings: IngestionSettings
    key_prefix: str = ""
    embedding_batch_size: int = 64
    producer_name: str = "ingestion-worker"
    usage_recorder: UsageRecorder | None = None
    embedding_usd_per_million_tokens: float | None = None


async def _record_embedding_usage(
    deps: IngestionDependencies,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    tokens: int | None,
) -> None:
    """Meter embedding spend (best effort; never blocks ingestion). Tokens are recorded only
    when the provider reported them; the cost is NULL unless a price is configured."""
    if deps.usage_recorder is None:
        return
    if tokens is not None:
        EMBEDDING_TOKENS.labels(deps.embedding_provider.model_name).inc(tokens)
    price = deps.embedding_usd_per_million_tokens
    cost = None if tokens is None or price is None else tokens * price / 1_000_000
    with usage_context(operation="ingestion", document_id=document_id):
        await deps.usage_recorder.record(
            UsageRecord(
                tenant_id=tenant_id,
                kind="embedding",
                provider=type(deps.embedding_provider).__name__,
                model=deps.embedding_provider.model_name,
                input_tokens=tokens,
                output_tokens=0,
                cost_usd=cost,
            )
        )


def _event(
    event_type: EventType,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    source: EventEnvelope,
    payload: dict[str, object],
    producer: str,
) -> EventEnvelope:
    return EventEnvelope(
        event_type=event_type,
        tenant_id=tenant_id,
        producer=producer,
        correlation_id=source.correlation_id,
        trace_id=source.trace_id,
        traceparent=current_traceparent() or source.traceparent,
        partition_key=document_partition_key(tenant_id=tenant_id, document_id=document_id),
        payload=payload,
    )


async def handle_document_ingested(deps: IngestionDependencies, event: EventEnvelope) -> None:
    """Consumer handler. Raises on failure so the consumer can retry or dead-letter."""
    parent = context_from_traceparent(event.traceparent)
    with span(
        "ingestion.handle_document_ingested",
        parent=parent,
        tenant_id=str(event.tenant_id),
        event_id=str(event.event_id),
    ):
        try:
            outcome = await _process(deps, event)
        except Exception as exc:
            INGESTION_DOCUMENTS.labels(
                "failed_retryable" if is_retryable(type(exc).__name__) else "failed_permanent"
            ).inc()
            await _record_failure(deps, event, exc)
            raise
        INGESTION_DOCUMENTS.labels(outcome.value).inc()


async def _process(deps: IngestionDependencies, event: EventEnvelope) -> IngestionOutcome:
    payload = DocumentIngestedPayload.model_validate(event.payload)
    tenant_id = event.tenant_id
    document_id = payload.document_id

    # ---- stage 1: claim ------------------------------------------------------------
    async with deps.sessionmaker() as session:
        doc = await _load_document(session, tenant_id, document_id)
        if doc.content_hash != payload.content_hash:
            raise InvalidDocument("Event content hash does not match the document record.")
        if doc.status in _TERMINAL_STATES:
            return IngestionOutcome.SKIPPED_TERMINAL
        if doc.status == DocumentStatus.active.value and await _version_exists(
            session, tenant_id, document_id, doc.content_hash
        ):
            return IngestionOutcome.ALREADY_INDEXED
        if doc.status != DocumentStatus.validating.value:  # `validating` = resume after a crash
            assert_valid_transition(doc.status, DocumentStatus.validating.value)
            doc.status = DocumentStatus.validating.value
        mime_type, filename, content_hash = doc.mime_type, doc.title, doc.content_hash
        await session.commit()

    # ---- stage 2: compute (no transaction open) -------------------------------------
    key = document_object_key(
        tenant_id=tenant_id,
        document_id=document_id,
        content_hash=content_hash,
        prefix=deps.key_prefix,
    )
    with timed(INGESTION_STAGE, stage="fetch"):
        content = await deps.store.get(key, max_bytes=deps.ingestion_settings.max_upload_size_bytes)
    if sha256_hex(content) != content_hash:
        await _quarantine(deps, tenant_id, document_id)
        raise InvalidDocument("Stored object does not match the recorded content hash.")
    validate_upload(
        filename=filename, mime_type=mime_type, content=content, settings=deps.ingestion_settings
    )
    with timed(INGESTION_STAGE, stage="parse_chunk"):
        chunks = await asyncio.to_thread(
            _parse_and_chunk, content, mime_type, deps.ingestion_settings
        )
    vectors: list[list[float]] = []
    reported_tokens: int | None = 0  # None once any batch fails to report usage
    with timed(INGESTION_STAGE, stage="embed"):
        for i in range(0, len(chunks), deps.embedding_batch_size):
            batch = [c.text for c in chunks[i : i + deps.embedding_batch_size]]
            if isinstance(deps.embedding_provider, MeteredEmbeddingProvider):
                metered = await deps.embedding_provider.embed_batch_metered(batch)
                got = metered.vectors
                reported_tokens = (
                    None
                    if (metered.input_tokens is None or reported_tokens is None)
                    else reported_tokens + metered.input_tokens
                )
            else:
                got = await deps.embedding_provider.embed_batch(batch)
                reported_tokens = None
            if len(got) != len(batch):
                raise InvalidDocument("Embedding provider returned a different number of vectors.")
            vectors.extend(got)
    await _record_embedding_usage(deps, tenant_id, document_id, reported_tokens)

    # ---- stage 3: one atomic write ---------------------------------------------------
    async with deps.sessionmaker() as session:
        doc = await _load_document(session, tenant_id, document_id, lock=True)
        if await _version_exists(session, tenant_id, document_id, content_hash):
            if doc.status != DocumentStatus.active.value:  # published by a racing worker
                doc.status = DocumentStatus.active.value
                await session.commit()
            return IngestionOutcome.ALREADY_INDEXED
        version = await create_draft_version(
            session, tenant_id=tenant_id, document_id=document_id, content_hash=content_hash
        )
        rows = await create_chunks_for_version(
            session, tenant_id=tenant_id, document_version_id=version.id, chunks=chunks
        )
        await create_embeddings_for_chunks(
            session,
            tenant_id=tenant_id,
            chunk_ids=[r.id for r in rows],
            vectors=vectors,
            embedding_model=deps.embedding_provider.model_name,
            embedding_version=_EMBEDDING_VERSION,
            provider=type(deps.embedding_provider).__name__,
        )
        await publish_version(
            session, tenant_id=tenant_id, document_id=document_id, version_number=version.version
        )
        for step in (
            DocumentStatus.processing,
            DocumentStatus.parsed,
            DocumentStatus.chunked,
            DocumentStatus.embedding,
            DocumentStatus.indexing,
            DocumentStatus.active,
        ):
            assert_valid_transition(doc.status, step.value)
            doc.status = step.value
        await enqueue_event(
            session,
            topic=Topics.DOCUMENT_PROCESSING_COMPLETED,
            event=_event(
                EventType.document_processing_completed,
                tenant_id,
                document_id,
                event,
                DocumentProcessingCompletedPayload(
                    document_id=document_id,
                    version=version.version,
                    chunk_count=len(rows),
                    embedding_model=deps.embedding_provider.model_name,
                ).model_dump(mode="json"),
                deps.producer_name,
            ),
        )
        await session.commit()
    _log.info(
        "document_indexed",
        tenant_id=str(tenant_id),
        document_id=str(document_id),
        chunks=len(chunks),
        version=version.version,
    )
    return IngestionOutcome.COMPLETED


def _parse_and_chunk(content: bytes, mime_type: str, settings: IngestionSettings) -> list[Chunk]:
    parser = get_parser_for_mime_type(mime_type)
    try:
        parsed: ParsedDocument = parser.parse(content)
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001 -- third-party parsers raise anything
        raise ParsingFailed("The document could not be parsed.") from exc
    validate_extracted_text(parsed.text, settings=settings)
    if not parsed.text.strip():
        raise InvalidDocument("The document contains no extractable text.")
    chunks = chunk_document(parsed)
    if not chunks:
        raise InvalidDocument("The document produced no chunks.")
    return chunks


async def _load_document(
    session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
) -> Document:
    stmt = select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
    if lock:
        stmt = stmt.with_for_update()
    doc = (await session.execute(stmt)).scalar_one_or_none()
    if doc is None:
        raise DocumentNotFound()
    return doc


async def _version_exists(
    session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID, content_hash: str
) -> bool:
    found = await session.execute(
        select(DocumentVersion.id).where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.content_hash == content_hash,
            DocumentVersion.status != DocumentVersionStatus.draft.value,
        )
    )
    return found.first() is not None


async def _quarantine(
    deps: IngestionDependencies, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    async with deps.sessionmaker() as session:
        doc = await _load_document(session, tenant_id, document_id)
        assert_valid_transition(doc.status, DocumentStatus.quarantined.value)
        doc.status = DocumentStatus.quarantined.value
        await session.commit()


async def _record_failure(
    deps: IngestionDependencies, event: EventEnvelope, exc: Exception
) -> None:
    """Best effort: a failure here must never mask the original error."""
    try:
        document_id = uuid.UUID(str(event.payload.get("document_id")))
    except ValueError:
        return  # malformed payload: nothing to attribute the failure to
    try:
        async with deps.sessionmaker() as session:
            doc = (
                await session.execute(
                    select(Document).where(
                        Document.tenant_id == event.tenant_id, Document.id == document_id
                    )
                )
            ).scalar_one_or_none()
            if doc is None:
                return
            if doc.status == DocumentStatus.validating.value:
                doc.status = DocumentStatus.processing_failed.value
            if not is_retryable(type(exc).__name__):
                await enqueue_event(
                    session,
                    topic=Topics.DOCUMENT_PROCESSING_FAILED,
                    event=_event(
                        EventType.document_processing_failed,
                        event.tenant_id,
                        document_id,
                        event,
                        DocumentProcessingFailedPayload(
                            document_id=document_id,
                            error_type=type(exc).__name__,
                            error_message=(exc.safe_message if isinstance(exc, DomainError) else "")
                            or type(exc).__name__,
                        ).model_dump(mode="json"),
                        deps.producer_name,
                    ),
                )
            await session.commit()
    except Exception:  # noqa: BLE001
        _log.exception("record_failure_failed", document_id=str(document_id))
