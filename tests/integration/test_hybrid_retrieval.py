"""
Proves hybrid search against a real Postgres: a chunk matched by BOTH retrieval legs
gets deduplicated (not returned twice) and ranks above a chunk matched by only one leg,
tenant isolation holds through the fused pipeline, and results respect `limit`.
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
from rag.retrieval.hybrid import hybrid_search
from sqlalchemy.ext.asyncio import AsyncSession

_DIMENSIONS = 1024


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
        title="Hybrid Test Doc",
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
async def test_chunk_matched_by_both_legs_ranks_first_and_is_not_duplicated(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "HybridBothLegsCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)

    exact_match_text = "The refund policy allows returns within thirty days of purchase."
    lexical_only_text = "Our refund process is handled by the finance team separately."
    vector_noise_text = "Completely unrelated content about quarterly engineering metrics."

    chunk_ids = await _seed_embedded_chunks(
        db_session,
        tenant,
        [exact_match_text, lexical_only_text, vector_noise_text],
        provider,
    )

    # Querying with the chunk's EXACT text guarantees it is the vector leg's #1 result
    # (deterministic provider: identical text -> identical vector -> zero distance) AND
    # naturally the lexical leg's top match too (it IS the text) -- a real, meaningful
    # "matched by both legs" scenario without relying on the deterministic provider
    # having any actual semantic understanding.
    results = await hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query=exact_match_text,
        embedding_provider=provider,
        limit=10,
    )

    result_ids = [chunk.id for chunk, _score in results]
    # No duplicates: each chunk id appears at most once.
    assert len(result_ids) == len(set(result_ids))
    # The double-matched chunk ranks first.
    assert result_ids[0] == chunk_ids[0]


@pytest.mark.integration
async def test_lexical_only_match_still_appears_via_fusion(db_session: AsyncSession) -> None:
    """A chunk with no vector-similarity signal but a strong lexical match must still
    surface -- proving fusion, not just the vector leg, drives inclusion."""
    tenant = await _make_tenant(db_session, "HybridLexOnlyCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)

    lexical_text = "Parental leave benefits are available to all full-time employees."
    unrelated_text = "The office coffee machine was replaced last Tuesday."

    chunk_ids = await _seed_embedded_chunks(
        db_session, tenant, [lexical_text, unrelated_text], provider
    )

    results = await hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query="parental leave benefits",
        embedding_provider=provider,
        limit=10,
    )
    result_ids = {chunk.id for chunk, _score in results}
    assert chunk_ids[0] in result_ids


@pytest.mark.integration
async def test_hybrid_search_respects_limit(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "HybridLimitCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    texts = [f"Refund policy document number {i} about returns." for i in range(8)]
    await _seed_embedded_chunks(db_session, tenant, texts, provider)

    results = await hybrid_search(
        db_session,
        tenant_id=tenant.id,
        query="refund policy returns",
        embedding_provider=provider,
        limit=3,
    )
    assert len(results) <= 3


@pytest.mark.integration
@pytest.mark.security
async def test_hybrid_search_is_tenant_scoped(db_session: AsyncSession) -> None:
    tenant_a = await _make_tenant(db_session, "HybridTenantA")
    tenant_b = await _make_tenant(db_session, "HybridTenantB")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)

    shared_text = "Confidential merger and acquisition negotiation details."
    ids_a = await _seed_embedded_chunks(db_session, tenant_a, [shared_text], provider)
    ids_b = await _seed_embedded_chunks(db_session, tenant_b, [shared_text], provider)

    results = await hybrid_search(
        db_session,
        tenant_id=tenant_a.id,
        query=shared_text,
        embedding_provider=provider,
        limit=10,
    )
    result_ids = {chunk.id for chunk, _score in results}
    assert result_ids == {ids_a[0]}
    assert ids_b[0] not in result_ids


@pytest.mark.integration
async def test_hybrid_search_with_no_matches_returns_empty(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "HybridEmptyCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    results = await hybrid_search(
        db_session, tenant_id=tenant.id, query="anything at all", embedding_provider=provider
    )
    assert results == []
