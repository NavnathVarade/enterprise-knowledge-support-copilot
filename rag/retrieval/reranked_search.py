"""
Reranked hybrid search: Phase 14's fused candidates -> reranker -> final order
(Section 27).

`RetrievalCandidate` carries every stage's score, not just the final one -- Section 27
is explicit that this is "critical for debugging retrieval quality," and Section 160's
"why did the answer fail?" tooling depends on exactly this kind of per-stage visibility
existing from the start rather than being bolted on when someone first needs to debug a
bad retrieval.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from packages.auth.identity import Identity
from packages.database.models.documents import DocumentChunk
from sqlalchemy.ext.asyncio import AsyncSession

from rag.embeddings.base import EmbeddingProvider
from rag.reranking.base import RerankCandidate, RerankerProvider
from rag.retrieval.hybrid import hybrid_search, hybrid_search_authorized


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk: DocumentChunk
    fused_score: float  # RRF score from Phase 14
    rerank_score: float


async def reranked_hybrid_search(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    embedding_provider: EmbeddingProvider,
    reranker: RerankerProvider,
    limit: int = 10,
    candidate_limit: int = 50,
) -> list[RetrievalCandidate]:
    """
    Retrieves more candidates than `limit` via hybrid_search (bounded by
    `candidate_limit`), reranks all of them, and returns the top `limit` by rerank
    score -- not by the original fused order. A candidate the fusion stage ranked low
    can still surface here if the reranker judges it more relevant; that's the entire
    point of a separate reranking stage (Section 27).
    """
    fused_results = await hybrid_search(
        session,
        tenant_id=tenant_id,
        query=query,
        embedding_provider=embedding_provider,
        limit=candidate_limit,
        candidate_limit=candidate_limit,
    )
    if not fused_results:
        return []

    fused_scores = {chunk.id: score for chunk, score in fused_results}
    chunks_by_id = {chunk.id: chunk for chunk, _score in fused_results}

    rerank_candidates = [
        RerankCandidate(chunk_id=chunk.id, text=chunk.text) for chunk, _score in fused_results
    ]
    rerank_results = await reranker.rerank(query, rerank_candidates)

    candidates = [
        RetrievalCandidate(
            chunk=chunks_by_id[result.chunk_id],
            fused_score=fused_scores[result.chunk_id],
            rerank_score=result.score,
        )
        for result in rerank_results
        if result.chunk_id in chunks_by_id
    ]
    # Defense in depth: do not assume a RerankerProvider implementation actually
    # honors its documented "sorted descending" contract (rag/reranking/base.py).
    # Explicitly re-sort by rerank_score here rather than trusting return order --
    # caught by test_reranking_genuinely_determines_final_order using a deliberately
    # contract-violating fake reranker, which would otherwise silently produce results
    # in the wrong order.
    candidates.sort(key=lambda c: -c.rerank_score)
    return candidates[:limit]


async def reranked_hybrid_search_authorized(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity: Identity,
    query: str,
    embedding_provider: EmbeddingProvider,
    reranker: RerankerProvider,
    limit: int = 10,
    candidate_limit: int = 50,
    as_of: datetime | None = None,
) -> list[RetrievalCandidate]:
    """
    THE function production code should call (Section 11, 16-17): ACL-aware,
    temporally-correct retrieval, reranked, with no unauthorized or temporally-
    ineffective chunk ever reaching this far -- both were filtered out at the SQL
    level in hybrid_search_authorized, before the reranker (and therefore before any
    future LLM call) ever sees their text.
    """
    fused_results = await hybrid_search_authorized(
        session,
        tenant_id=tenant_id,
        identity=identity,
        query=query,
        embedding_provider=embedding_provider,
        limit=candidate_limit,
        candidate_limit=candidate_limit,
        as_of=as_of,
    )
    if not fused_results:
        return []

    fused_scores = {chunk.id: score for chunk, score in fused_results}
    chunks_by_id = {chunk.id: chunk for chunk, _score in fused_results}

    rerank_candidates = [
        RerankCandidate(chunk_id=chunk.id, text=chunk.text) for chunk, _score in fused_results
    ]
    rerank_results = await reranker.rerank(query, rerank_candidates)

    candidates = [
        RetrievalCandidate(
            chunk=chunks_by_id[result.chunk_id],
            fused_score=fused_scores[result.chunk_id],
            rerank_score=result.score,
        )
        for result in rerank_results
        if result.chunk_id in chunks_by_id
    ]
    candidates.sort(key=lambda c: -c.rerank_score)
    return candidates[:limit]
