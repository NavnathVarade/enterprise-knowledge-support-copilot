"""
THE critical security test suite for this entire project (Section 11, 13, 127): proves
that authorization is enforced IN THE RETRIEVAL QUERY, not as a filter applied to
already-fetched results. Every test here follows the same shape -- seed a document with
a specific ACL, seed an authorized and an unauthorized Identity, and assert the
unauthorized identity's query result set does not contain the restricted chunk AT ALL
(not "ranked low," not "flagged," ABSENT) -- across every retrieval function this system
has: vector, lexical, hybrid, and reranked hybrid.
"""

from __future__ import annotations

import uuid

import pytest
from packages.auth.identity import Identity
from packages.config.settings import get_settings
from packages.database.models import Group, Role, Tenant, User
from packages.database.repositories.chunks import create_chunks_for_version
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
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from rag.retrieval.hybrid import hybrid_search_authorized
from rag.retrieval.reranked_search import reranked_hybrid_search_authorized
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


def _identity(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    roles: frozenset[str] = frozenset(),
    groups: frozenset[str] = frozenset(),
    department: str | None = None,
) -> Identity:
    return Identity(
        user_id=user_id or uuid.uuid4(),
        tenant_id=tenant_id,
        email="test@example.com",
        roles=roles,
        groups=groups,
        department=department,
    )


async def _seed_chunk_with_acl(
    session: AsyncSession,
    tenant: Tenant,
    *,
    text: str,
    provider: DeterministicLocalEmbeddingProvider,
    acl_kwargs: dict | None,
) -> uuid.UUID:
    """acl_kwargs=None means NO acl row at all (fail-closed default case)."""
    document = await create_document(
        session,
        tenant_id=tenant.id,
        title=f"Doc {uuid.uuid4().hex[:8]}",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text(text),
    )
    version = await create_draft_version(
        session, tenant_id=tenant.id, document_id=document.id, content_hash=sha256_hex_of_text(text)
    )
    version = await publish_version(
        session, tenant_id=tenant.id, document_id=document.id, version_number=version.version
    )
    chunk_rows = await create_chunks_for_version(
        session,
        tenant_id=tenant.id,
        document_version_id=version.id,
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
    chunk_id = chunk_rows[0].id

    vector = await provider.embed_query(text)
    await create_embeddings_for_chunks(
        session,
        tenant_id=tenant.id,
        chunk_ids=[chunk_id],
        vectors=[vector],
        embedding_model=provider.model_name,
        embedding_version="v1",
        provider="deterministic-local",
    )

    if acl_kwargs is not None:
        await create_acl_entry(session, tenant_id=tenant.id, document_id=document.id, **acl_kwargs)

    await session.commit()
    return chunk_id


@pytest.mark.integration
@pytest.mark.security
class TestVectorSearchAuthorization:
    async def test_role_based_acl_grants_access_to_matching_role(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session, "VecRoleCo")
        provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
        role = Role(tenant_id=tenant.id, name="security_auditor")
        db_session.add(role)
        await db_session.flush()

        text = "CONFIDENTIAL incident response escalation contacts."
        chunk_id = await _seed_chunk_with_acl(
            db_session,
            tenant,
            text=text,
            provider=provider,
            acl_kwargs={"visibility": "role_based", "allowed_role_id": role.id},
        )

        authorized = _identity(tenant_id=tenant.id, roles=frozenset({"security_auditor"}))
        unauthorized = _identity(tenant_id=tenant.id, roles=frozenset({"engineer"}))

        vector = await provider.embed_query(text)
        auth_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=authorized, query_vector=vector, limit=10
        )
        unauth_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=unauthorized, query_vector=vector, limit=10
        )

        assert chunk_id in {e.chunk_id for e, _ in auth_results}
        assert chunk_id not in {e.chunk_id for e, _ in unauth_results}
        assert unauth_results == []  # the ONLY chunk in this tenant, entirely absent

    async def test_user_specific_acl_grants_access_only_to_that_user(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session, "VecUserCo")
        provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
        text = "CONFIDENTIAL executive compensation figures."

        # allowed_user_id has a real foreign key to users.id (Section 54's schema) --
        # a user_specific grant can only ever reference a user that actually exists,
        # so the authorized identity needs a real User row, not just a random UUID.
        authorized_user = User(
            tenant_id=tenant.id,
            email="authorized@example.com",
            hashed_password="x",
            display_name="Authorized User",
        )
        db_session.add(authorized_user)
        await db_session.flush()

        authorized = _identity(tenant_id=tenant.id, user_id=authorized_user.id)
        unauthorized = _identity(tenant_id=tenant.id)

        chunk_id = await _seed_chunk_with_acl(
            db_session,
            tenant,
            text=text,
            provider=provider,
            acl_kwargs={"visibility": "user_specific", "allowed_user_id": authorized.user_id},
        )

        vector = await provider.embed_query(text)
        auth_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=authorized, query_vector=vector, limit=10
        )
        unauth_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=unauthorized, query_vector=vector, limit=10
        )
        assert chunk_id in {e.chunk_id for e, _ in auth_results}
        assert unauth_results == []

    async def test_department_only_acl(self, db_session: AsyncSession) -> None:
        tenant = await _make_tenant(db_session, "VecDeptCo")
        provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
        text = "HR-only internal headcount planning notes."

        chunk_id = await _seed_chunk_with_acl(
            db_session,
            tenant,
            text=text,
            provider=provider,
            acl_kwargs={"visibility": "department_only", "department": "HR"},
        )

        hr_identity = _identity(tenant_id=tenant.id, department="HR")
        eng_identity = _identity(tenant_id=tenant.id, department="Engineering")
        no_dept_identity = _identity(tenant_id=tenant.id, department=None)

        vector = await provider.embed_query(text)
        hr_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=hr_identity, query_vector=vector, limit=10
        )
        eng_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=eng_identity, query_vector=vector, limit=10
        )
        no_dept_results = await find_nearest_chunks_authorized(
            db_session,
            tenant_id=tenant.id,
            identity=no_dept_identity,
            query_vector=vector,
            limit=10,
        )

        assert chunk_id in {e.chunk_id for e, _ in hr_results}
        assert eng_results == []
        assert no_dept_results == []

    async def test_group_only_acl(self, db_session: AsyncSession) -> None:
        tenant = await _make_tenant(db_session, "VecGroupCo")
        provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
        group = Group(tenant_id=tenant.id, name="finance-leads")
        db_session.add(group)
        await db_session.flush()

        text = "Finance leadership budget allocation draft."
        chunk_id = await _seed_chunk_with_acl(
            db_session,
            tenant,
            text=text,
            provider=provider,
            acl_kwargs={"visibility": "group_only", "allowed_group_id": group.id},
        )

        member = _identity(tenant_id=tenant.id, groups=frozenset({"finance-leads"}))
        non_member = _identity(tenant_id=tenant.id, groups=frozenset({"engineering"}))

        vector = await provider.embed_query(text)
        member_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=member, query_vector=vector, limit=10
        )
        non_member_results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=non_member, query_vector=vector, limit=10
        )
        assert chunk_id in {e.chunk_id for e, _ in member_results}
        assert non_member_results == []

    async def test_public_within_tenant_visible_to_everyone(self, db_session: AsyncSession) -> None:
        tenant = await _make_tenant(db_session, "VecPublicCo")
        provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
        text = "Public company holiday schedule."
        chunk_id = await _seed_chunk_with_acl(
            db_session,
            tenant,
            text=text,
            provider=provider,
            acl_kwargs={"visibility": "public_within_tenant"},
        )

        anyone = _identity(tenant_id=tenant.id)  # no roles, no groups, no department
        vector = await provider.embed_query(text)
        results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=anyone, query_vector=vector, limit=10
        )
        assert chunk_id in {e.chunk_id for e, _ in results}

    async def test_no_acl_row_is_invisible_to_everyone_fail_closed(
        self, db_session: AsyncSession
    ) -> None:
        """The absence of a grant is not an implicit grant."""
        tenant = await _make_tenant(db_session, "VecNoAclCo")
        provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
        text = "A document nobody has been explicitly granted access to."
        chunk_id = await _seed_chunk_with_acl(
            db_session, tenant, text=text, provider=provider, acl_kwargs=None
        )

        even_an_admin = _identity(tenant_id=tenant.id, roles=frozenset({"admin"}))
        vector = await provider.embed_query(text)
        results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=even_an_admin, query_vector=vector, limit=10
        )
        assert results == []
        assert chunk_id not in {e.chunk_id for e, _ in results}

    async def test_restricted_visibility_has_no_automatic_grant(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session, "VecRestrictedCo")
        provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
        text = "A document explicitly marked restricted."
        await _seed_chunk_with_acl(
            db_session,
            tenant,
            text=text,
            provider=provider,
            acl_kwargs={"visibility": "restricted"},
        )

        anyone = _identity(tenant_id=tenant.id, roles=frozenset({"admin"}))
        vector = await provider.embed_query(text)
        results = await find_nearest_chunks_authorized(
            db_session, tenant_id=tenant.id, identity=anyone, query_vector=vector, limit=10
        )
        assert results == []


@pytest.mark.integration
@pytest.mark.security
async def test_lexical_search_authorized_excludes_unauthorized_chunk(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "LexAuthCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    role = Role(tenant_id=tenant.id, name="security_auditor")
    db_session.add(role)
    await db_session.flush()

    text = "Confidential security incident containment runbook."
    chunk_id = await _seed_chunk_with_acl(
        db_session,
        tenant,
        text=text,
        provider=provider,
        acl_kwargs={"visibility": "role_based", "allowed_role_id": role.id},
    )

    authorized = _identity(tenant_id=tenant.id, roles=frozenset({"security_auditor"}))
    unauthorized = _identity(tenant_id=tenant.id, roles=frozenset({"support_agent"}))

    auth_results = await search_chunks_lexical_authorized(
        db_session, tenant_id=tenant.id, identity=authorized, query="confidential security incident"
    )
    unauth_results = await search_chunks_lexical_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=unauthorized,
        query="confidential security incident",
    )

    assert chunk_id in {c.id for c, _ in auth_results}
    assert unauth_results == []


@pytest.mark.integration
@pytest.mark.security
async def test_hybrid_search_authorized_excludes_unauthorized_chunk_from_fused_results(
    db_session: AsyncSession,
) -> None:
    """Combines vector + lexical -- proves the ACL filter survives fusion, not just
    each leg in isolation."""
    tenant = await _make_tenant(db_session, "HybridAuthCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    role = Role(tenant_id=tenant.id, name="security_auditor")
    db_session.add(role)
    await db_session.flush()

    restricted_text = "CONFIDENTIAL merger negotiation terms and valuation."
    public_text = "Public holiday calendar for the upcoming year."

    restricted_chunk_id = await _seed_chunk_with_acl(
        db_session,
        tenant,
        text=restricted_text,
        provider=provider,
        acl_kwargs={"visibility": "role_based", "allowed_role_id": role.id},
    )
    await _seed_chunk_with_acl(
        db_session,
        tenant,
        text=public_text,
        provider=provider,
        acl_kwargs={"visibility": "public_within_tenant"},
    )

    unauthorized = _identity(tenant_id=tenant.id, roles=frozenset({"support_agent"}))
    authorized = _identity(tenant_id=tenant.id, roles=frozenset({"security_auditor"}))

    unauth_results = await hybrid_search_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=unauthorized,
        query="confidential merger negotiation valuation",
        embedding_provider=provider,
        limit=10,
    )
    auth_results = await hybrid_search_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=authorized,
        query="confidential merger negotiation valuation",
        embedding_provider=provider,
        limit=10,
    )

    unauth_ids = {c.id for c, _ in unauth_results}
    auth_ids = {c.id for c, _ in auth_results}
    # THE assertion that matters: the restricted chunk must be completely absent for
    # the unauthorized identity, regardless of how relevant it is to the query.
    # (public chunk MAY legitimately appear for the unauthorized identity too --
    # hybrid_search has no relevance threshold, so an authorized candidate can surface
    # even with low topical relevance when it's one of the only authorized chunks
    # available. That's correct retrieval behavior, not an ACL concern.)
    assert restricted_chunk_id not in unauth_ids
    assert restricted_chunk_id in auth_ids


@pytest.mark.integration
@pytest.mark.security
async def test_reranked_search_authorized_never_surfaces_unauthorized_chunk(
    db_session: AsyncSession,
) -> None:
    """The full production path: ACL-aware hybrid retrieval + reranking. Even a
    reranker that would otherwise rank the restricted chunk #1 by pure lexical overlap
    must never see it, because it was excluded before reranking ever ran."""
    tenant = await _make_tenant(db_session, "RerankAuthCo")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)
    role = Role(tenant_id=tenant.id, name="security_auditor")
    db_session.add(role)
    await db_session.flush()

    restricted_text = "refund refund refund policy policy policy thirty thirty days days"
    weaker_public_text = "The refund policy allows returns within a reasonable window."

    restricted_chunk_id = await _seed_chunk_with_acl(
        db_session,
        tenant,
        text=restricted_text,
        provider=provider,
        acl_kwargs={"visibility": "role_based", "allowed_role_id": role.id},
    )
    public_chunk_id = await _seed_chunk_with_acl(
        db_session,
        tenant,
        text=weaker_public_text,
        provider=provider,
        acl_kwargs={"visibility": "public_within_tenant"},
    )

    unauthorized = _identity(tenant_id=tenant.id, roles=frozenset({"support_agent"}))
    results = await reranked_hybrid_search_authorized(
        db_session,
        tenant_id=tenant.id,
        identity=unauthorized,
        query="refund policy thirty days",
        embedding_provider=provider,
        reranker=LexicalOverlapReranker(),
        limit=10,
    )

    result_ids = {c.chunk.id for c in results}
    assert restricted_chunk_id not in result_ids
    assert public_chunk_id in result_ids  # the one chunk this identity CAN see


@pytest.mark.integration
@pytest.mark.security
async def test_colliding_role_names_across_tenants_do_not_leak_access(
    db_session: AsyncSession,
) -> None:
    """Two different tenants each have a role named 'security_auditor' -- same name,
    different Role rows. Tenant B's auditor, correctly querying Tenant B, must never
    see Tenant A's document restricted to Tenant A's identically-named role."""
    tenant_a = await _make_tenant(db_session, "RoleCollideTenantA")
    tenant_b = await _make_tenant(db_session, "RoleCollideTenantB")
    provider = DeterministicLocalEmbeddingProvider(dimensions=_DIMENSIONS)

    role_a = Role(tenant_id=tenant_a.id, name="security_auditor")
    role_b = Role(tenant_id=tenant_b.id, name="security_auditor")
    db_session.add_all([role_a, role_b])
    await db_session.flush()

    text = "Tenant A's confidential document, restricted to Tenant A's auditors."
    chunk_id = await _seed_chunk_with_acl(
        db_session,
        tenant_a,
        text=text,
        provider=provider,
        acl_kwargs={"visibility": "role_based", "allowed_role_id": role_a.id},
    )

    tenant_b_auditor = _identity(tenant_id=tenant_b.id, roles=frozenset({"security_auditor"}))
    vector = await provider.embed_query(text)

    results = await find_nearest_chunks_authorized(
        db_session,
        tenant_id=tenant_b.id,
        identity=tenant_b_auditor,
        query_vector=vector,
        limit=10,
    )
    assert chunk_id not in {e.chunk_id for e, _ in results}
    assert results == []
