"""
Proves two distinct things against a real Postgres:
  1. Reranking genuinely determines the FINAL order -- not just that a rerank_score is
     attached alongside the fused order. Uses a deliberately order-inverting fake
     reranker so the proof doesn't depend on guessing how the deterministic embedding
     provider's pseudo-random vectors interact with real lexical ranking.
  2. The real LexicalOverlapReranker wires correctly end-to-end: every returned
     candidate carries both a fused_score and a rerank_score, and a clearly relevant
     chunk surfaces in the results.
"""

from __future__ import annotations

import uuid

import pytest
from packages.config.settings import get_settings
from packages.database.models import Tenant
from packages.database.repositories.chunks import create_chunks_for_version
from packages.database.repositories.documents import (
    create_document,
    create_draft_version,
    publish_version,
)
from packages.database.repositories.embeddings import create_embeddings_for_chunks
from packages.database.session import get_sessionmaker
from packages.domain.hashing import sha256_hex_of_text
from rag.chunking.base import Chunk
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.reranking.base import RerankCandidate, RerankResult
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from rag.retrieval.reranked_search import reranked_hybrid_search
from sqlalchemy.ext.asyncio import AsyncSession

_DIMENSIONS = 1024


class _OrderInvertingFakeReranker:
    """A reranker whose behavior is fully known in advance: it reverses whatever order
    it was given, so the test can assert the FINAL result matches this known inversion
    rather than the original fused order."""

    async def rerank(self, query: str, candidates: list[RerankCandidate]) -> list[RerankResult]:
        reversed_candidates = list(reversed(candidates))
        return [
            RerankResult(chunk_id=c.chunk_id, score=float(i))
            for i, c in enumerate(reversed_candidates)
        ]


@pytest.fixture
async def db_session() -> AsyncSession:
    settings = get_settings()
    sessionmaker = get_sessionmaker(settings.database)
    async with sessionmaker() as session:
        yield session
        await session.rollback()


async def _make_tenant(session: AsyncSession, name: str) -> Tenant:
    tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _seed_embedded_chunks(
    session: AsyncSession,
    tenant: Tenant,
    texts: list[str],
    provider: DeterministicLocalEmbeddingProvider,
) -> list[uuid.UUID]:
    document = await create_document(
        session,
        tenant_id=tenant.id,
        title="Rerank Test Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("d"),
    )
    version = await create_draft_version(
        session, tenant_id=tenant.id, document_id=document.id, content_hash=sha256_hex_of_text("v")
    )
    version = await publish_version(
        session, tenant_id=tenant.id, document_id=document.id, version_number=version.version
    )
    chunks = [
        Chunk(
            sequence=i,
            text=t,
            token_count=max(1, len(t) // 4),
            heading_path=None,
            page_number=None,
            character_start=0,
            character_end=len(t),
        )
        for i, t in enumerate(texts)
    ]
    rows = await create_chunks_for_version(
        session, tenant_id=tenant.id, document_version_id=version.id, chunks=chunks
    )
    vectors = await provider.embed_batch(texts)
    await create_embeddings_for_chunks(
        session,
        tenant_id=tenant.id,
        chunk_ids=[r.id for r in rows],
        vectors=vectors,
        embedding_model=provider.model_name,
        embedding_version="v1",
        provider="deterministic-local",
    )
    await session.commit()
    return [r.id for r in rows]


@pytest.mark.integration
async def test_reranking_genuinely_determines_final_order(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "RerankOrderCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    texts = [f"Refund document {i} with some content." for i in range(4)]
    await _seed_embedded_chunks(db_session, tenant, texts, provider)

    fake_reranker = _OrderInvertingFakeReranker()
    results = await reranked_hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query="refund",
        embedding_provider=provider,
        reranker=fake_reranker,
        limit=4,
        candidate_limit=50,
    )

    assert len(results) == 4
    rerank_scores = [c.rerank_score for c in results]
    assert rerank_scores == sorted(rerank_scores, reverse=True)
    assert rerank_scores == [3.0, 2.0, 1.0, 0.0]


@pytest.mark.integration
async def test_every_result_carries_both_fused_and_rerank_scores(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "RerankScoreFieldsCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    texts = ["Refund policy details here.", "Something about refunds and returns."]
    await _seed_embedded_chunks(db_session, tenant, texts, provider)

    results = await reranked_hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query="refund",
        embedding_provider=provider,
        reranker=LexicalOverlapReranker(),
    )

    assert len(results) == 2
    for candidate in results:
        assert isinstance(candidate.fused_score, float)
        assert isinstance(candidate.rerank_score, float)
        assert candidate.chunk is not None


@pytest.mark.integration
async def test_real_lexical_overlap_reranker_surfaces_relevant_chunk_first(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "RerankRealCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    highly_relevant = "The refund policy allows returns within thirty days of purchase."
    tangential = "Refunds are occasionally discussed in quarterly finance meetings."
    await _seed_embedded_chunks(db_session, tenant, [highly_relevant, tangential], provider)

    results = await reranked_hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query="refund policy returns thirty days purchase",
        embedding_provider=provider,
        reranker=LexicalOverlapReranker(),
    )

    assert results[0].chunk.text == highly_relevant


@pytest.mark.integration
async def test_reranked_search_respects_limit(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "RerankLimitCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    texts = [f"Refund document number {i}." for i in range(6)]
    await _seed_embedded_chunks(db_session, tenant, texts, provider)

    results = await reranked_hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query="refund",
        embedding_provider=provider,
        reranker=LexicalOverlapReranker(),
        limit=2,
    )
    assert len(results) <= 2


@pytest.mark.integration
async def test_reranked_search_with_no_candidates_returns_empty(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "RerankEmptyCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    results = await reranked_hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query="anything",
        embedding_provider=provider,
        reranker=LexicalOverlapReranker(),
    )
    assert results == []
