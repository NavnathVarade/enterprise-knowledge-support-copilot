"""
Documents routes.

`POST /documents` (Phase 7): validates the upload (Section 88), computes its content
hash, creates a Document row with status='uploaded', and GRANTS THE UPLOADER
user_specific ACL access to it (Phase 16) -- then returns immediately (202 Accepted)
without running any processing pipeline, per Section 83/48's "the API must not block
for long-running document processing" rule. The ACL grant matters because this system
is fail-closed (Section 11): a document with zero document_acl rows is authorized to
NOBODY, including its own uploader, so without this grant a user's own freshly uploaded
document would be invisible to them immediately after upload.

`GET /documents` (Phase 4, now Phase 16): lists a tenant's documents the CALLER IS
AUTHORIZED TO SEE -- the authorization predicate is applied in the SQL query itself
(packages.database.repositories.authorization.build_document_acl_predicate), not as a
post-hoc filter on an already-fetched list.
"""

from __future__ import annotations

import uuid
from uuid import UUID

from apps.api.app.dependencies.auth import get_current_identity
from apps.api.app.dependencies.db import get_tenant_session
from fastapi import APIRouter, Depends, File, Request, UploadFile
from packages.audit.service import record_audit
from packages.auth.identity import Identity
from packages.database.models import Document
from packages.database.repositories.authorization import build_document_acl_predicate
from packages.database.repositories.documents import create_acl_entry, create_document
from packages.database.repositories.outbox import enqueue_event
from packages.domain.errors import ConfigurationError
from packages.domain.hashing import sha256_hex
from packages.events.schemas import DocumentIngestedPayload, EventEnvelope, EventType
from packages.kafka.topics import Topics, document_partition_key
from packages.observability.tracing import current_traceparent
from packages.storage.object_store import ObjectStore, build_object_store, document_object_key
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


class DocumentSummary(BaseModel):
    id: UUID
    title: str
    status: str


class DocumentUploadResponse(BaseModel):
    id: UUID
    title: str
    status: str
    content_hash: str


def get_object_store(request: Request) -> ObjectStore:
    """Cached per app. Tests override this dependency; there is deliberately NO silent
    in-memory fallback -- an unconfigured bucket is a configuration error, not a default."""
    cached: ObjectStore | None = getattr(request.app.state, "object_store", None)
    if cached is not None:
        return cached
    settings = request.app.state.settings
    if not settings.object_store.bucket and not settings.object_store.local_directory:
        raise ConfigurationError("OBJECT_STORE_BUCKET is not configured.")
    store = build_object_store(settings.object_store)
    request.app.state.object_store = store
    return store


@router.get("", response_model=list[DocumentSummary])
async def list_documents(
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_tenant_session),
) -> list[DocumentSummary]:
    # tenant_id comes ONLY from the verified Identity (Section 9), AND the ACL
    # predicate is applied in the same query -- an unauthorized document is never
    # fetched, let alone filtered out afterward (Section 11, 13).
    result = await session.execute(
        select(Document).where(
            Document.tenant_id == identity.tenant_id,
            build_document_acl_predicate(identity.tenant_id, identity),
        )
    )
    documents = result.scalars().all()
    return [DocumentSummary(id=d.id, title=d.title, status=d.status) for d in documents]


@router.post("", response_model=DocumentUploadResponse, status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_tenant_session),
    store: ObjectStore = Depends(get_object_store),
) -> DocumentUploadResponse:
    from rag.ingestion.validation import validate_upload

    settings = request.app.state.settings
    content = await file.read()
    mime_type = file.content_type or "application/octet-stream"
    filename = file.filename or ""

    # Raises a specific DomainError (mapped to a safe HTTP response by the app-level
    # exception handler) on the first failing check -- filename, then MIME type, then
    # size (Section 88). Nothing here yet sniffs actual file content against the
    # claimed MIME type; see rag/ingestion/validation.py's module docstring for the
    # explicit list of what this phase does NOT cover.
    validate_upload(
        filename=filename, mime_type=mime_type, content=content, settings=settings.ingestion
    )

    document = await create_document(
        session,
        tenant_id=identity.tenant_id,
        title=filename,
        source_type="upload",
        mime_type=mime_type,
        content_hash=sha256_hex(content),
        owner_id=identity.user_id,
    )
    # Original bytes go to object storage BEFORE the commit, under a key derived from
    # (tenant, document, hash). If the commit then fails, an orphan object remains (storage
    # is cheaper than losing the upload; an orphan sweeper is a future job). If the put
    # fails, nothing is committed.
    key = document_object_key(
        tenant_id=identity.tenant_id,
        document_id=document.id,
        content_hash=document.content_hash,
        prefix=settings.object_store.key_prefix,
    )
    await store.put(key, content, content_type=mime_type)
    document.source_uri = store.uri_for(key)
    # Fail-closed ACL (Section 11): without an explicit grant, NOBODY can see a document,
    # including its uploader, so grant the uploader direct access.
    await create_acl_entry(
        session,
        tenant_id=identity.tenant_id,
        document_id=document.id,
        visibility="user_specific",
        allowed_user_id=identity.user_id,
    )
    # Transactional outbox: the `document.ingested` event commits atomically with the row.
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    await enqueue_event(
        session,
        topic=Topics.DOCUMENT_INGESTED,
        event=EventEnvelope(
            event_type=EventType.document_ingested,
            tenant_id=identity.tenant_id,
            producer="api",
            traceparent=current_traceparent(),
            correlation_id=request_id,
            trace_id=request_id,
            partition_key=document_partition_key(
                tenant_id=identity.tenant_id, document_id=document.id
            ),
            payload=DocumentIngestedPayload(
                document_id=document.id,
                content_hash=document.content_hash,
                mime_type=mime_type,
                filename=filename,
                size_bytes=len(content),
            ).model_dump(mode="json"),
        ),
    )
    # Audited in the SAME transaction: an upload that is not on the audit trail cannot commit.
    await record_audit(
        session,
        tenant_id=identity.tenant_id,
        actor_user_id=identity.user_id,
        action="document.upload",
        resource_type="document",
        resource_id=str(document.id),
        outcome="success",
        request_id=request_id,
        details={"mime_type": mime_type, "size_bytes": len(content)},
    )
    await session.commit()

    return DocumentUploadResponse(
        id=document.id,
        title=document.title,
        status=document.status,
        content_hash=document.content_hash,
    )
