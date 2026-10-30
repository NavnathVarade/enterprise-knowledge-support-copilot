"""
End-to-end proof that embeddings flow correctly from the deterministic provider through
to a real pgvector similarity query: embed real chunks, persist them, and verify that
querying with the EXACT text of one chunk returns that chunk as the nearest neighbor
with (near-)zero cosine distance -- a real, meaningful assertion even though the
deterministic provider carries no real semantic content.
"""

from __future__ import annotations

import uuid

import pytest
from packages.config.settings import get_settings
from packages.database.models import Tenant
from packages.database.models.documents import Embedding
from packages.database.repositories.chunks import create_chunks_for_version
from packages.database.repositories.documents import (
    create_document,
    create_draft_version,
    publish_version,
)
from packages.database.repositories.embeddings import (
    create_embeddings_for_chunks,
    find_nearest_chunks,
)
from packages.database.session import get_sessionmaker
from packages.domain.hashing import sha256_hex_of_text
from rag.chunking.base import Chunk
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_REAL_COLUMN_DIMENSIONS = 1024  # must match packages/database/models/documents.py::_EMBEDDING_DIM


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


async def _make_version_with_chunks(
    session: AsyncSession, tenant: Tenant, texts: list[str]
) -> list[uuid.UUID]:
    document = await create_document(
        session,
        tenant_id=tenant.id,
        title="Embedding Test Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("doc"),
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
            text=text,
            token_count=len(text) // 4,
            heading_path=None,
            page_number=None,
            character_start=0,
            character_end=len(text),
        )
        for i, text in enumerate(texts)
    ]
    persisted = await create_chunks_for_version(
        session, tenant_id=tenant.id, document_version_id=version.id, chunks=chunks
    )
    return [c.id for c in persisted]


@pytest.mark.integration
async def test_full_embedding_pipeline_and_exact_match_retrieval(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "EmbedPipelineCo")

    texts = [
        "The refund policy allows returns within 30 days.",
        "Our engineering team deploys on Fridays.",
        "Parental leave is 16 weeks paid.",
    ]
    chunk_ids = await _make_version_with_chunks(db_session, tenant, texts)

    provider = DeterministicLocalEmbeddingProvider(dimensions=_REAL_COLUMN_DIMENSIONS)
    vectors = await provider.embed_batch(texts)

    await create_embeddings_for_chunks(
        db_session,
        tenant_id=tenant.id,
        chunk_ids=chunk_ids,
        vectors=vectors,
        embedding_model=provider.model_name,
        embedding_version="v1",
        provider="deterministic-local",
    )
    await db_session.commit()

    query_vector = await provider.embed_query("Our engineering team deploys on Fridays.")
    results = await find_nearest_chunks(
        db_session, tenant_id=tenant.id, query_vector=query_vector, limit=3
    )

    assert len(results) == 3
    nearest_embedding, nearest_distance = results[0]
    assert nearest_embedding.chunk_id == chunk_ids[1]
    assert nearest_distance == pytest.approx(0.0, abs=1e-4)

    assert results[1][1] > nearest_distance
    assert results[2][1] > nearest_distance


@pytest.mark.integration
async def test_dimension_mismatch_is_rejected_by_the_database(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "DimMismatchCo")
    chunk_ids = await _make_version_with_chunks(db_session, tenant, ["one chunk of text"])

    wrong_dimension_vector = [0.1] * 512  # column is fixed at 1024

    with pytest.raises(DBAPIError, match="expected 1024 dimensions, not 512"):
        await create_embeddings_for_chunks(
            db_session,
            tenant_id=tenant.id,
            chunk_ids=chunk_ids,
            vectors=[wrong_dimension_vector],
            embedding_model="test-model",
            embedding_version="v1",
            provider="test",
        )
        await db_session.flush()


@pytest.mark.integration
@pytest.mark.security
async def test_embeddings_are_tenant_isolated_at_the_schema_level(db_session: AsyncSession) -> None:
    tenant_a = await _make_tenant(db_session, "EmbedTenantA")
    tenant_b = await _make_tenant(db_session, "EmbedTenantB")

    chunk_ids_a = await _make_version_with_chunks(db_session, tenant_a, ["tenant a content"])

    malicious_embedding = Embedding(
        tenant_id=tenant_b.id,
        chunk_id=chunk_ids_a[0],
        embedding=[0.1] * _REAL_COLUMN_DIMENSIONS,
        embedding_model="test",
        embedding_version="v1",
        provider="test",
    )
    db_session.add(malicious_embedding)

    with pytest.raises(IntegrityError, match="fk_embeddings_chunk"):
        await db_session.flush()


@pytest.mark.integration
async def test_find_nearest_chunks_is_tenant_scoped(db_session: AsyncSession) -> None:
    tenant_a = await _make_tenant(db_session, "SearchTenantA")
    tenant_b = await _make_tenant(db_session, "SearchTenantB")

    provider = DeterministicLocalEmbeddingProvider(dimensions=_REAL_COLUMN_DIMENSIONS)

    chunk_ids_a = await _make_version_with_chunks(db_session, tenant_a, ["shared search term"])
    chunk_ids_b = await _make_version_with_chunks(db_session, tenant_b, ["shared search term"])

    vector = await provider.embed_query("shared search term")
    await create_embeddings_for_chunks(
        db_session,
        tenant_id=tenant_a.id,
        chunk_ids=chunk_ids_a,
        vectors=[vector],
        embedding_model=provider.model_name,
        embedding_version="v1",
        provider="deterministic-local",
    )
    await create_embeddings_for_chunks(
        db_session,
        tenant_id=tenant_b.id,
        chunk_ids=chunk_ids_b,
        vectors=[vector],
        embedding_model=provider.model_name,
        embedding_version="v1",
        provider="deterministic-local",
    )
    await db_session.commit()

    results = await find_nearest_chunks(
        db_session, tenant_id=tenant_a.id, query_vector=vector, limit=10
    )
    result_chunk_ids = {r[0].chunk_id for r in results}
    assert result_chunk_ids == {chunk_ids_a[0]}
    assert chunk_ids_b[0] not in result_chunk_ids
