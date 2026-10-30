"""
Embedding persistence (Section 24).

`create_embeddings_for_chunks` maps EmbeddingProvider output onto the real pgvector
column, tied to `chunk_id` via the same composite tenant-aware foreign key pattern used
throughout this schema. A vector whose length doesn't match the column's fixed
dimension (Section 23) is rejected by pgvector itself at insert time, not silently
truncated or padded.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.auth.identity import Identity
from packages.config.settings import get_settings
from packages.database.models.documents import Document, DocumentChunk, DocumentVersion, Embedding
from packages.database.repositories.authorization import build_document_acl_predicate
from packages.database.repositories.temporal import build_version_temporal_predicate
from packages.domain.errors import DocumentNotFound


async def create_embeddings_for_chunks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    chunk_ids: list[uuid.UUID],
    vectors: list[list[float]],
    embedding_model: str,
    embedding_version: str,
    provider: str,
) -> list[Embedding]:
    if len(chunk_ids) != len(vectors):
        raise ValueError(
            f"chunk_ids ({len(chunk_ids)}) and vectors ({len(vectors)}) must be the same length."
        )

    result = await session.execute(
        select(DocumentChunk.id).where(
            DocumentChunk.tenant_id == tenant_id, DocumentChunk.id.in_(chunk_ids)
        )
    )
    found_ids = set(result.scalars().all())
    missing = set(chunk_ids) - found_ids
    if missing:
        raise DocumentNotFound(f"Chunk(s) not found for this tenant: {missing}")

    rows: list[Embedding] = []
    for chunk_id, vector in zip(chunk_ids, vectors, strict=True):
        row = Embedding(
            tenant_id=tenant_id,
            chunk_id=chunk_id,
            embedding=vector,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            provider=provider,
        )
        session.add(row)
        rows.append(row)

    await session.flush()
    return rows


async def apply_hnsw_query_settings(session: AsyncSession) -> None:
    """
    Applies RETRIEVAL_HNSW_* as transaction-local settings (set_config(..., is_local=true)),
    so they never leak to other work on a pooled connection. No-op when none are configured.
    """
    cfg = get_settings().retrieval
    for name, value in (
        ("hnsw.ef_search", cfg.hnsw_ef_search),
        ("hnsw.iterative_scan", cfg.hnsw_iterative_scan),
        ("hnsw.max_scan_tuples", cfg.hnsw_max_scan_tuples),
    ):
        if value is not None:
            await session.execute(select(func.set_config(name, str(value), True)))


async def find_nearest_chunks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query_vector: list[float],
    limit: int = 10,
) -> list[tuple[Embedding, float]]:
    """
    Returns (Embedding, cosine_distance) pairs, nearest first, using pgvector's `<=>`
    cosine-distance operator. Tenant-scoped by the WHERE clause -- this is Phase 13's
    real ACL/hybrid retrieval query in miniature, proving the vector column and index
    from Phase 3 actually work end-to-end before that phase builds on top of it.
    """
    await apply_hnsw_query_settings(session)
    distance = Embedding.embedding.cosine_distance(cast(query_vector, Vector(len(query_vector))))
    result = await session.execute(
        select(Embedding, distance.label("distance"))
        .where(Embedding.tenant_id == tenant_id)
        .order_by(distance)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]


async def find_nearest_chunks_authorized(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity: Identity,
    query_vector: list[float],
    limit: int = 10,
    as_of: datetime | None = None,
) -> list[tuple[Embedding, float]]:
    """
    THE ACL-aware, temporally-correct retrieval query (Section 11, 13, 16-17): joins
    Embedding -> DocumentChunk -> DocumentVersion -> Document and ANDs BOTH the
    authorization predicate AND the temporal-effectiveness predicate into the WHERE
    clause alongside tenant scoping. An unauthorized document's chunks are never
    fetched from Postgres at all -- not retrieved-then-discarded -- and neither are a
    draft's chunks, nor (by default, as_of=None means "now") a superseded version's
    chunks. Pass an explicit past `as_of` to resolve historical content instead.
    """
    await apply_hnsw_query_settings(session)
    distance = Embedding.embedding.cosine_distance(cast(query_vector, Vector(len(query_vector))))
    result = await session.execute(
        select(Embedding, distance.label("distance"))
        .join(DocumentChunk, DocumentChunk.id == Embedding.chunk_id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Embedding.tenant_id == tenant_id,
            build_document_acl_predicate(tenant_id, identity),
            build_version_temporal_predicate(as_of),
        )
        .order_by(distance)
        .limit(limit)
    )
    return [(row[0], row[1]) for row in result.all()]
