"""
Chunk persistence (Section 21: "every retrieved chunk must be traceable back to
document version -> source artifact -> source system").

`create_chunks_for_version` is the join between Phase 10's pure chunking algorithm and
Phase 3's schema: it persists each `Chunk` as a `DocumentChunk` row bound to a specific
`document_version_id` via the composite tenant-aware foreign key, so the database
itself refuses a chunk that claims to belong to the wrong tenant's version, exactly as
it does for document_versions -> documents.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.auth.identity import Identity
from packages.database.models.documents import DocumentChunk, DocumentVersion, DocumentVersionStatus
from packages.domain.chunk import Chunk
from packages.domain.errors import DocumentNotFound
from packages.domain.hashing import sha256_hex_of_text


async def create_chunks_for_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_version_id: uuid.UUID,
    chunks: list[Chunk],
) -> list[DocumentChunk]:
    result = await session.execute(
        select(DocumentVersion).where(
            DocumentVersion.tenant_id == tenant_id, DocumentVersion.id == document_version_id
        )
    )
    if result.scalar_one_or_none() is None:
        raise DocumentNotFound(f"Document version {document_version_id} not found.")

    rows: list[DocumentChunk] = []
    for chunk in chunks:
        row = DocumentChunk(
            tenant_id=tenant_id,
            document_version_id=document_version_id,
            sequence=chunk.sequence,
            text=chunk.text,
            token_count=chunk.token_count,
            heading_path=chunk.heading_path,
            page_number=chunk.page_number,
            char_start=chunk.character_start,
            char_end=chunk.character_end,
            content_hash=sha256_hex_of_text(chunk.text),
            chunking_version=chunk.chunking_version,
        )
        session.add(row)
        rows.append(row)

    await session.flush()
    return rows


async def get_chunks_for_version(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_version_id: uuid.UUID
) -> list[DocumentChunk]:
    result = await session.execute(
        select(DocumentChunk)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.document_version_id == document_version_id,
        )
        .order_by(DocumentChunk.sequence)
    )
    return list(result.scalars().all())


async def get_chunks_for_version_comparison(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity: Identity,
    document_id: uuid.UUID,
    version_a: int,
    version_b: int,
) -> tuple[list[DocumentChunk], list[DocumentChunk]]:
    """
    "What changed between v2 and v3?" (Section 16): fetches BOTH versions' chunks for
    comparison, deliberately bypassing the temporal-effectiveness predicate (a
    comparison explicitly wants a version that may no longer be current) while still
    fully enforcing the ACL predicate (Section 11) -- comparing versions of a document
    the caller isn't authorized to see must still return nothing, not a bypass of
    authorization just because the request shape is "comparison" rather than "search".
    """
    from packages.database.models.documents import Document
    from packages.database.repositories.authorization import build_document_acl_predicate

    async def _chunks_for(version_number: int) -> list[DocumentChunk]:
        result = await session.execute(
            select(DocumentChunk)
            .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentChunk.tenant_id == tenant_id,
                DocumentVersion.document_id == document_id,
                DocumentVersion.version == version_number,
                # A draft was never in effect: comparison bypasses the *effective-window*
                # predicate (superseded versions are fair game) but must never expose
                # unpublished content.
                DocumentVersion.status != DocumentVersionStatus.draft.value,
                build_document_acl_predicate(tenant_id, identity),
            )
            .order_by(DocumentChunk.sequence)
        )
        return list(result.scalars().all())

    chunks_a = await _chunks_for(version_a)
    chunks_b = await _chunks_for(version_b)
    return chunks_a, chunks_b
