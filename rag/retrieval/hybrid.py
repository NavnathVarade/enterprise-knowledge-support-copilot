"""
Hybrid retrieval (Section 25): dense (pgvector) + lexical (Postgres full-text) ->
dedup -> Reciprocal Rank Fusion -> final ranked chunk list.

Deduplication is a natural consequence of RRF's scoring (a chunk appearing in both
retrievers' results gets both contributions summed under the SAME id, not two separate
entries), not a separate step bolted on afterward -- this is why Section 25's "do not
simply concatenate two result lists" concern doesn't apply here: concatenation would
produce duplicates, RRF over unique ids cannot.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from packages.auth.identity import Identity
from packages.database.models.documents import DocumentChunk
from packages.database.repositories.embeddings import (
    find_nearest_chunks,
    find_nearest_chunks_authorized,
)
from packages.database.repositories.lexical_search import (
    search_chunks_lexical,
    search_chunks_lexical_authorized,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag.embeddings.base import EmbeddingProvider
from rag.retrieval.fusion import reciprocal_rank_fusion


async def hybrid_search(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    embedding_provider: EmbeddingProvider,
    limit: int = 10,
    candidate_limit: int = 50,
    rrf_k: int = 60,
) -> list[tuple[DocumentChunk, float]]:
    """
    Returns (chunk, fused_score) pairs, best match first. `candidate_limit` bounds how
    many results each individual retriever contributes BEFORE fusion (Section 27's
    "Top-K Candidates" going into reranking downstream) -- it is deliberately larger
    than `limit`, the final number of results returned, so fusion has enough material
    from each leg to combine meaningfully.
    """
    query_vector = await embedding_provider.embed_query(query)

    vector_results = await find_nearest_chunks(
        session, tenant_id=tenant_id, query_vector=query_vector, limit=candidate_limit
    )
    lexical_results = await search_chunks_lexical(
        session, tenant_id=tenant_id, query=query, limit=candidate_limit
    )

    vector_ids = [embedding.chunk_id for embedding, _distance in vector_results]
    lexical_ids = [chunk.id for chunk, _score in lexical_results]

    fused = reciprocal_rank_fusion([vector_ids, lexical_ids], k=rrf_k)
    top_ids = [item_id for item_id, _score in fused[:limit]]
    if not top_ids:
        return []

    result = await session.execute(
        select(DocumentChunk).where(
            DocumentChunk.tenant_id == tenant_id, DocumentChunk.id.in_(top_ids)
        )
    )
    chunks_by_id = {chunk.id: chunk for chunk in result.scalars().all()}
    fused_scores = dict(fused)

    # Preserve fusion order -- select(...).where(id.in_(...)) does not guarantee
    # result ordering matches the `in_` list.
    return [
        (chunks_by_id[item_id], fused_scores[item_id])
        for item_id in top_ids
        if item_id in chunks_by_id
    ]


async def hybrid_search_authorized(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity: Identity,
    query: str,
    embedding_provider: EmbeddingProvider,
    limit: int = 10,
    candidate_limit: int = 50,
    rrf_k: int = 60,
    as_of: datetime | None = None,
) -> list[tuple[DocumentChunk, float]]:
    """
    ACL-aware, temporally-correct counterpart to hybrid_search (Section 11, 13, 16-17):
    both retrieval legs use their _authorized variants (which already apply the ACL
    AND temporal predicates), so unauthorized or temporally-ineffective chunks never
    enter the fused candidate set in the first place. The final bulk load by id ALSO
    re-applies both predicates, joined through DocumentVersion/Document -- defense in
    depth (Section 11's "also perform defense-in-depth authorization after
    retrieval"), not because the legs are expected to leak, but because a
    single-layer guarantee is exactly the pattern this whole system refuses to rely
    on everywhere else.
    """
    from packages.database.models.documents import Document, DocumentVersion
    from packages.database.repositories.authorization import build_document_acl_predicate
    from packages.database.repositories.temporal import build_version_temporal_predicate

    query_vector = await embedding_provider.embed_query(query)

    vector_results = await find_nearest_chunks_authorized(
        session,
        tenant_id=tenant_id,
        identity=identity,
        query_vector=query_vector,
        limit=candidate_limit,
        as_of=as_of,
    )
    lexical_results = await search_chunks_lexical_authorized(
        session,
        tenant_id=tenant_id,
        identity=identity,
        query=query,
        limit=candidate_limit,
        as_of=as_of,
    )

    vector_ids = [embedding.chunk_id for embedding, _distance in vector_results]
    lexical_ids = [chunk.id for chunk, _score in lexical_results]

    fused = reciprocal_rank_fusion([vector_ids, lexical_ids], k=rrf_k)
    top_ids = [item_id for item_id, _score in fused[:limit]]
    if not top_ids:
        return []

    result = await session.execute(
        select(DocumentChunk)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.id.in_(top_ids),
            build_document_acl_predicate(tenant_id, identity),
            build_version_temporal_predicate(as_of),
        )
    )
    chunks_by_id = {chunk.id: chunk for chunk in result.scalars().all()}
    fused_scores = dict(fused)

    return [
        (chunks_by_id[item_id], fused_scores[item_id])
        for item_id in top_ids
        if item_id in chunks_by_id
    ]
