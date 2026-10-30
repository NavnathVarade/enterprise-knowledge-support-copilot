"""
Proves Phase 13's lexical search against a real Postgres: relevance ordering, stemming,
safe handling of hostile query strings, tenant isolation, and that the GIN index is
actually chosen by the query planner rather than merely existing.
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
from packages.database.repositories.lexical_search import search_chunks_lexical
from packages.database.session import get_sessionmaker
from packages.domain.hashing import sha256_hex_of_text
from rag.chunking.base import Chunk
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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


async def _seed_chunks(session: AsyncSession, tenant: Tenant, texts: list[str]) -> list[uuid.UUID]:
    document = await create_document(
        session,
        tenant_id=tenant.id,
        title="Lexical Test Doc",
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
    await session.commit()
    return [r.id for r in rows]


@pytest.mark.integration
async def test_matching_chunk_is_returned_and_unrelated_is_not(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "LexBasicCo")
    ids = await _seed_chunks(
        db_session,
        tenant,
        [
            "Customers may request a refund within thirty days of purchase.",
            "The engineering team deploys the service on Fridays.",
        ],
    )

    results = await search_chunks_lexical(db_session, tenant_id=tenant.id, query="refund policy")
    found = [chunk.id for chunk, _ in results]
    assert ids[0] in found
    assert ids[1] not in found


@pytest.mark.integration
async def test_more_relevant_chunk_ranks_higher(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "LexRankCo")
    ids = await _seed_chunks(
        db_session,
        tenant,
        [
            "Refund refund refund: the refund window for a refund request is thirty days.",
            "Please see the appendix; a refund may be mentioned somewhere near the end.",
        ],
    )
    results = await search_chunks_lexical(db_session, tenant_id=tenant.id, query="refund")
    assert [c.id for c, _ in results][0] == ids[0]
    assert results[0][1] > results[1][1]


@pytest.mark.integration
async def test_chunk_matching_more_query_terms_outranks_one_matching_fewer(
    db_session: AsyncSession,
) -> None:
    """Under OR semantics a partial match is admitted, but a fuller match must rank higher."""
    tenant = await _make_tenant(db_session, "LexMultiTermCo")
    ids = await _seed_chunks(
        db_session,
        tenant,
        [
            "The refund policy allows returns within thirty days.",
            "Refunds are processed by the finance team.",
        ],
    )
    results = await search_chunks_lexical(
        db_session, tenant_id=tenant.id, query="refund policy returns"
    )
    ordered = [c.id for c, _ in results]
    assert set(ordered) == {ids[0], ids[1]}  # recall: both admitted
    assert ordered[0] == ids[0]  # precision: the fuller match ranks first


@pytest.mark.integration
async def test_query_of_only_stop_words_returns_nothing_without_error(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "LexStopWordCo")
    await _seed_chunks(db_session, tenant, ["the and of to a in"])
    assert await search_chunks_lexical(db_session, tenant_id=tenant.id, query="the and of") == []


@pytest.mark.integration
async def test_stemming_matches_word_variants(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "LexStemCo")
    ids = await _seed_chunks(db_session, tenant, ["Employees are entitled to paid leaves."])
    # "entitle" / "leave" should match "entitled" / "leaves" via English stemming.
    results = await search_chunks_lexical(db_session, tenant_id=tenant.id, query="entitle leave")
    assert ids[0] in [c.id for c, _ in results]


@pytest.mark.integration
async def test_empty_and_whitespace_queries_return_nothing(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "LexEmptyCo")
    await _seed_chunks(db_session, tenant, ["some content"])
    assert await search_chunks_lexical(db_session, tenant_id=tenant.id, query="") == []
    assert await search_chunks_lexical(db_session, tenant_id=tenant.id, query="   ") == []


@pytest.mark.integration
@pytest.mark.security
@pytest.mark.parametrize(
    "hostile_query",
    [
        "'; DROP TABLE document_chunks; --",
        '"unbalanced quote',
        "refund & | ! ( ) :*",
        "a" * 2000,
        "\\",
    ],
)
async def test_hostile_query_strings_never_raise_or_damage(
    db_session: AsyncSession, hostile_query: str
) -> None:
    tenant = await _make_tenant(db_session, "LexHostileCo")
    await _seed_chunks(db_session, tenant, ["ordinary content about refunds"])

    # Must not raise (websearch_to_tsquery tolerates arbitrary input) ...
    await search_chunks_lexical(db_session, tenant_id=tenant.id, query=hostile_query)

    # ... and the table must still exist and be intact afterwards.
    count = (await db_session.execute(text("SELECT count(*) FROM document_chunks"))).scalar_one()
    assert count >= 1


@pytest.mark.integration
@pytest.mark.security
async def test_lexical_search_is_tenant_scoped(db_session: AsyncSession) -> None:
    tenant_a = await _make_tenant(db_session, "LexTenantA")
    tenant_b = await _make_tenant(db_session, "LexTenantB")
    ids_a = await _seed_chunks(db_session, tenant_a, ["confidential merger acquisition plans"])
    ids_b = await _seed_chunks(db_session, tenant_b, ["confidential merger acquisition plans"])

    results_a = await search_chunks_lexical(db_session, tenant_id=tenant_a.id, query="merger")
    found = {c.id for c, _ in results_a}
    assert found == {ids_a[0]}
    assert ids_b[0] not in found


@pytest.mark.integration
async def test_gin_index_is_used_by_the_planner(db_session: AsyncSession) -> None:
    """
    An index that exists but is never chosen is worthless. Force the planner's hand on a
    tiny table (enable_seqscan=off) and confirm the GIN index is a valid plan for this
    exact predicate -- if the column or index were mis-defined, this would fall back to a
    sequential scan and the assertion would fail.
    """
    tenant = await _make_tenant(db_session, "LexIndexCo")
    await _seed_chunks(db_session, tenant, ["indexed content about refunds"])

    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan_rows = (
        await db_session.execute(
            text(
                "EXPLAIN SELECT id FROM document_chunks "
                "WHERE text_search @@ websearch_to_tsquery('english', 'refund')"
            )
        )
    ).all()
    plan = "\n".join(r[0] for r in plan_rows)
    assert "ix_document_chunks_text_search" in plan, plan
