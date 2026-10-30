"""
Proves Section 16-17's temporal retrieval guarantee survives the FULL pipeline --
not just Phase 5's document-version resolution in isolation, but chunking, embedding,
and retrieval all respecting "what's current" vs "what was true then" correctly, with
ACL enforcement still intact alongside it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from packages.auth.identity import Identity
from packages.config.settings import get_settings
from packages.database.models import Role, Tenant
from packages.database.repositories.chunks import (
    create_chunks_for_version,
    get_chunks_for_version_comparison,
)
from packages.database.repositories.documents import (
    create_acl_entry,
    create_document,
    create_draft_version,
    publish_version,
)
from packages.database.repositories.embeddings import (
    create_embeddings_for_chunks,
    find_nearest_chunks_authorized,
)
from packages.database.repositories.lexical_search import search_chunks_lexical_authorized
from packages.database.session import get_sessionmaker
from packages.domain.hashing import sha256_hex_of_text
from rag.chunking.base import Chunk
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.retrieval.hybrid import hybrid_search_authorized
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


def _identity(tenant_id: uuid.UUID) -> Identity:
    return Identity(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        email="t@example.com",
        roles=frozenset(),
        groups=frozenset(),
    )


async def _chunk_and_embed_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_version_id: uuid.UUID,
    text: str,
    provider: DeterministicLocalEmbeddingProvider,
) -> uuid.UUID:
    rows = await create_chunks_for_version(
        session,
        tenant_id=tenant_id,
        document_version_id=document_version_id,
        chunks=[
            Chunk(
                sequence=0,
                text=text,
                token_count=max(1, len(text) // 4),
                heading_path=None,
                page_number=None,
                character_start=0,
                character_end=len(text),
            )
        ],
    )
    chunk_id = rows[0].id
    vector = await provider.embed_query(text)
    await create_embeddings_for_chunks(
        session,
        tenant_id=tenant_id,
        chunk_ids=[chunk_id],
        vectors=[vector],
        embedding_model=provider.model_name,
        embedding_version="v1",
        provider="deterministic-local",
    )
    return chunk_id


@pytest.mark.integration
async def test_default_retrieval_returns_only_current_version_not_superseded(
    db_session: AsyncSession,
) -> None:
    """THE central proof: v1 and v2 are both chunked and embedded (as a real pipeline
    would do), but default retrieval (as_of=None -> now) must return ONLY v2's chunk."""
    tenant = await _make_tenant(db_session, "TemporalCurrentCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    identity = _identity(tenant.id)

    document = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Refund Policy",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("v1"),
    )
    await create_acl_entry(
        db_session, tenant_id=tenant.id, document_id=document.id, visibility="public_within_tenant"
    )

    now = datetime.now(UTC)
    t0 = now - timedelta(days=60)
    t1 = now - timedelta(days=5)

    v1_text = "Refund policy version one: returns within fourteen days."
    v1 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v1_text),
    )
    v1 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=v1.version,
        effective_from=t0,
    )
    v1_chunk_id = await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v1.id, text=v1_text, provider=provider
    )

    v2_text = "Refund policy version two: returns within thirty days."
    v2 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v2_text),
    )
    v2 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=v2.version,
        effective_from=t1,
    )
    v2_chunk_id = await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v2.id, text=v2_text, provider=provider
    )
    await db_session.commit()

    vector = await provider.embed_query("refund policy returns days")
    current_results = await find_nearest_chunks_authorized(
        db_session, tenant_id=tenant.id, identity=identity, query_vector=vector, limit=10
    )
    current_ids = {e.chunk_id for e, _ in current_results}

    assert v2_chunk_id in current_ids
    assert v1_chunk_id not in current_ids  # superseded -- must not appear by default


@pytest.mark.integration
async def test_historical_as_of_resolves_to_the_version_in_effect_then(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "TemporalHistoricalCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    identity = _identity(tenant.id)

    document = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Security Policy",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("v1"),
    )
    await create_acl_entry(
        db_session, tenant_id=tenant.id, document_id=document.id, visibility="public_within_tenant"
    )

    now = datetime.now(UTC)
    t0 = now - timedelta(days=100)
    t1 = now - timedelta(days=10)

    v1_text = "Security policy version one: passwords rotate every ninety days."
    v1 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v1_text),
    )
    v1 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=v1.version,
        effective_from=t0,
    )
    v1_chunk_id = await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v1.id, text=v1_text, provider=provider
    )

    v2_text = "Security policy version two: passwords rotate every thirty days."
    v2 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v2_text),
    )
    v2 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=v2.version,
        effective_from=t1,
    )
    v2_chunk_id = await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v2.id, text=v2_text, provider=provider
    )
    await db_session.commit()

    vector = await provider.embed_query("security policy password rotation")
    historical_as_of = t0 + timedelta(days=5)  # squarely within v1's effective window
    historical_results = await find_nearest_chunks_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=identity,
        query_vector=vector,
        limit=10,
        as_of=historical_as_of,
    )
    historical_ids = {e.chunk_id for e, _ in historical_results}

    assert v1_chunk_id in historical_ids
    assert v2_chunk_id not in historical_ids  # not yet effective at that past instant


@pytest.mark.integration
async def test_draft_version_chunks_never_returned_regardless_of_as_of(
    db_session: AsyncSession,
) -> None:
    """A draft is never in effect at any instant -- not now, not at any past as_of."""
    tenant = await _make_tenant(db_session, "TemporalDraftCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    identity = _identity(tenant.id)

    document = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Draft Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("d"),
    )
    await create_acl_entry(
        db_session, tenant_id=tenant.id, document_id=document.id, visibility="public_within_tenant"
    )

    draft_text = "This content is still in draft and has never been published."
    draft = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(draft_text),
    )
    draft_chunk_id = await _chunk_and_embed_version(
        db_session,
        tenant_id=tenant.id,
        document_version_id=draft.id,
        text=draft_text,
        provider=provider,
    )
    await db_session.commit()

    vector = await provider.embed_query("still in draft never published")
    now_results = await find_nearest_chunks_authorized(
        db_session, tenant_id=tenant.id, identity=identity, query_vector=vector, limit=10
    )
    far_past_results = await find_nearest_chunks_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=identity,
        query_vector=vector,
        limit=10,
        as_of=datetime.now(UTC) - timedelta(days=3650),
    )

    assert draft_chunk_id not in {e.chunk_id for e, _ in now_results}
    assert draft_chunk_id not in {e.chunk_id for e, _ in far_past_results}


@pytest.mark.integration
async def test_lexical_and_hybrid_search_also_respect_temporal_filtering(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "TemporalHybridCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    identity = _identity(tenant.id)

    document = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="API Docs",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("v1"),
    )
    await create_acl_entry(
        db_session, tenant_id=tenant.id, document_id=document.id, visibility="public_within_tenant"
    )

    now = datetime.now(UTC)
    v1_text = "API endpoint documentation legacy version one deprecated."
    v1 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v1_text),
    )
    v1 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=v1.version,
        effective_from=now - timedelta(days=90),
    )
    v1_chunk_id = await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v1.id, text=v1_text, provider=provider
    )

    v2_text = "API endpoint documentation current version two active."
    v2 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v2_text),
    )
    v2 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        version_number=v2.version,
        effective_from=now - timedelta(days=1),
    )
    v2_chunk_id = await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v2.id, text=v2_text, provider=provider
    )
    await db_session.commit()

    lexical_results = await search_chunks_lexical_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=identity,
        query="API endpoint documentation version",
    )
    lexical_ids = {c.id for c, _ in lexical_results}
    assert v2_chunk_id in lexical_ids
    assert v1_chunk_id not in lexical_ids

    hybrid_results = await hybrid_search_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=identity,
        query="API endpoint documentation version",
        embedding_provider=provider,
    )
    hybrid_ids = {c.id for c, _ in hybrid_results}
    assert v2_chunk_id in hybrid_ids
    assert v1_chunk_id not in hybrid_ids


@pytest.mark.integration
async def test_version_comparison_returns_both_versions_chunks(db_session: AsyncSession) -> None:
    """'What changed between v1 and v2?' -- comparison deliberately bypasses the
    temporal filter (it explicitly wants a non-current version) but still enforces ACL."""
    tenant = await _make_tenant(db_session, "TemporalComparisonCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    identity = _identity(tenant.id)

    document = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Comparison Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("v1"),
    )
    await create_acl_entry(
        db_session, tenant_id=tenant.id, document_id=document.id, visibility="public_within_tenant"
    )

    v1_text = "Version one content about vacation policy: ten days."
    v1 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v1_text),
    )
    v1 = await publish_version(
        db_session, tenant_id=tenant.id, document_id=document.id, version_number=v1.version
    )
    await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v1.id, text=v1_text, provider=provider
    )

    v2_text = "Version two content about vacation policy: twenty days."
    v2 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v2_text),
    )
    v2 = await publish_version(
        db_session, tenant_id=tenant.id, document_id=document.id, version_number=v2.version
    )
    await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v2.id, text=v2_text, provider=provider
    )
    await db_session.commit()

    chunks_v1, chunks_v2 = await get_chunks_for_version_comparison(
        db_session,
        tenant_id=tenant.id,
        identity=identity,
        document_id=document.id,
        version_a=v1.version,
        version_b=v2.version,
    )

    assert len(chunks_v1) == 1
    assert len(chunks_v2) == 1
    assert "ten days" in chunks_v1[0].text
    assert "twenty days" in chunks_v2[0].text


@pytest.mark.integration
@pytest.mark.security
async def test_version_comparison_respects_acl(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "TemporalComparisonAclCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    role = Role(tenant_id=tenant.id, name="security_auditor")
    db_session.add(role)
    await db_session.flush()

    document = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Restricted Comparison Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("v1"),
    )
    await create_acl_entry(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        visibility="role_based",
        allowed_role_id=role.id,
    )

    v1_text = "Restricted version one."
    v1 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v1_text),
    )
    v1 = await publish_version(
        db_session, tenant_id=tenant.id, document_id=document.id, version_number=v1.version
    )
    await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v1.id, text=v1_text, provider=provider
    )

    v2_text = "Restricted version two."
    v2 = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(v2_text),
    )
    v2 = await publish_version(
        db_session, tenant_id=tenant.id, document_id=document.id, version_number=v2.version
    )
    await _chunk_and_embed_version(
        db_session, tenant_id=tenant.id, document_version_id=v2.id, text=v2_text, provider=provider
    )
    await db_session.commit()

    unauthorized = _identity(tenant.id)
    chunks_v1, chunks_v2 = await get_chunks_for_version_comparison(
        db_session,
        tenant_id=tenant.id,
        identity=unauthorized,
        document_id=document.id,
        version_a=v1.version,
        version_b=v2.version,
    )
    assert chunks_v1 == []
    assert chunks_v2 == []
