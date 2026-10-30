"""
Document and DocumentVersion repository.

The temporal resolution logic here (`get_current_version`, `get_version_effective_at`)
is the concrete implementation of Section 16's rule: "current" is derived from
`effective_from`/`effective_until`/`status='published'`, never from upload order or
`created_at`. A document can have version 3 be the most recently uploaded row while
version 2 is still the one actually in effect (e.g. a policy scheduled for a future
effective date).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.documents import (
    Document,
    DocumentACL,
    DocumentVersion,
    DocumentVersionStatus,
)
from packages.domain.document_lifecycle import assert_valid_transition
from packages.domain.errors import DocumentNotFound, InvalidDocument


async def get_document_by_title(
    session: AsyncSession, *, tenant_id: uuid.UUID, title: str
) -> Document | None:
    result = await session.execute(
        select(Document).where(Document.tenant_id == tenant_id, Document.title == title)
    )
    return result.scalar_one_or_none()


async def create_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    title: str,
    source_type: str,
    mime_type: str,
    content_hash: str,
    description: str | None = None,
    department: str | None = None,
    security_classification: str = "INTERNAL",
    owner_id: uuid.UUID | None = None,
    source_uri: str | None = None,
) -> Document:
    document = Document(
        tenant_id=tenant_id,
        title=title,
        source_type=source_type,
        mime_type=mime_type,
        content_hash=content_hash,
        description=description,
        department=department,
        security_classification=security_classification,
        owner_id=owner_id,
        source_uri=source_uri,
    )
    session.add(document)
    await session.flush()
    return document


async def transition_document_status(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID, target_status: str
) -> Document:
    document = await _get_document_or_raise(session, tenant_id=tenant_id, document_id=document_id)
    assert_valid_transition(document.status, target_status)
    document.status = target_status
    await session.flush()
    return document


async def create_draft_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    content_hash: str,
) -> DocumentVersion:
    await _get_document_or_raise(session, tenant_id=tenant_id, document_id=document_id)

    result = await session.execute(
        select(DocumentVersion.version)
        .where(DocumentVersion.tenant_id == tenant_id, DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version.desc())
        .limit(1)
    )
    latest_version_number = result.scalar_one_or_none() or 0

    version = DocumentVersion(
        tenant_id=tenant_id,
        document_id=document_id,
        version=latest_version_number + 1,
        status=DocumentVersionStatus.draft.value,
        content_hash=content_hash,
    )
    session.add(version)
    await session.flush()
    return version


async def publish_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_number: int,
    effective_from: datetime | None = None,
) -> DocumentVersion:
    """
    Publishes a draft version and supersedes whichever version was previously the
    "current" published one (Section 15: "never simply overwrite enterprise
    knowledge" -- the old version's row is kept, just marked superseded with a
    recorded `effective_until` and `superseded_at`, not deleted or mutated away).
    """
    effective_from = effective_from or datetime.now(UTC)

    result = await session.execute(
        select(DocumentVersion).where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.version == version_number,
        )
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise DocumentNotFound(f"Version {version_number} of document {document_id} not found.")
    if version.status != DocumentVersionStatus.draft.value:
        raise InvalidDocument(
            f"Only a draft version can be published (current status: {version.status})."
        )

    # Supersede whatever was previously current, if anything.
    previous = await get_current_version(
        session, tenant_id=tenant_id, document_id=document_id, as_of=effective_from
    )
    if previous is not None and previous.id != version.id:
        previous.status = DocumentVersionStatus.superseded.value
        previous.effective_until = effective_from
        previous.superseded_at = effective_from

    version.status = DocumentVersionStatus.published.value
    version.published_at = effective_from
    version.effective_from = effective_from
    await session.flush()
    return version


async def get_current_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    as_of: datetime | None = None,
) -> DocumentVersion | None:
    """
    The version that was/is in effect at `as_of` (default: now): `effective_from <=
    as_of` and (`effective_until` is NULL or `> as_of`), among versions that were ever
    actually published. Explicitly NOT "the highest version number", NOT "the most
    recently created row", and NOT restricted to status=='published' in the row's
    CURRENT state -- a version that was published and has since been superseded still
    represents a real historical effective period and must be returned for a query
    about that period (Section 16: "historical questions must retrieve historically
    effective versions"). Only 'draft' versions (never actually in effect) are excluded.
    """
    as_of = as_of or datetime.now(UTC)
    result = await session.execute(
        select(DocumentVersion)
        .where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.status != DocumentVersionStatus.draft.value,
            DocumentVersion.effective_from <= as_of,
        )
        .order_by(DocumentVersion.effective_from.desc())
        .limit(1)
    )
    candidate = result.scalar_one_or_none()
    if candidate is None:
        return None
    if candidate.effective_until is not None and candidate.effective_until <= as_of:
        return None
    return candidate


async def get_version_by_number(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID, version_number: int
) -> DocumentVersion | None:
    result = await session.execute(
        select(DocumentVersion).where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.version == version_number,
        )
    )
    return result.scalar_one_or_none()


async def advance_document_to_active(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """
    Drives a freshly-uploaded document through the full processing pipeline to
    'active' in one call, via the same state machine every real ingestion job uses
    (packages.domain.document_lifecycle). Used by seeding/tests that need an active
    document without running an actual parse/chunk/embed/index pipeline (Phases 7-12).
    """
    for target in (
        "validating",
        "processing",
        "parsed",
        "chunked",
        "embedding",
        "indexing",
        "active",
    ):
        await transition_document_status(
            session, tenant_id=tenant_id, document_id=document_id, target_status=target
        )
    result = await session.execute(
        select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
    )
    document = result.scalar_one()
    return document


async def create_acl_entry(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    visibility: str,
    department: str | None = None,
    allowed_user_id: uuid.UUID | None = None,
    allowed_group_id: uuid.UUID | None = None,
    allowed_role_id: uuid.UUID | None = None,
) -> DocumentACL:
    acl = DocumentACL(
        tenant_id=tenant_id,
        document_id=document_id,
        visibility=visibility,
        department=department,
        allowed_user_id=allowed_user_id,
        allowed_group_id=allowed_group_id,
        allowed_role_id=allowed_role_id,
    )
    session.add(acl)
    await session.flush()
    return acl


async def _get_document_or_raise(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    result = await session.execute(
        select(Document).where(Document.tenant_id == tenant_id, Document.id == document_id)
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise DocumentNotFound()
    return document
