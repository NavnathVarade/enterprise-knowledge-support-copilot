"""
These tests prove, against a real Postgres, the specific invariants Phase 3 exists to
deliver: composite tenant-aware foreign keys reject cross-tenant references (the
database itself, not just application code, refuses to let a chunk/version/ACL row
point at a different tenant's document), and CHECK constraints reject invalid
enum/visibility data even bypassing the ORM's own validation.
"""

from __future__ import annotations

import uuid

import pytest
from packages.config.settings import get_settings
from packages.database.models import Document, DocumentACL, DocumentVersion, Tenant
from packages.database.session import get_sessionmaker
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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


@pytest.mark.integration
async def test_document_created_successfully(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "Acme")
    doc = Document(
        tenant_id=tenant.id,
        source_type="upload",
        title="Refund Policy",
        mime_type="application/pdf",
        content_hash="a" * 64,
    )
    db_session.add(doc)
    await db_session.flush()
    assert doc.id is not None
    assert doc.status == "uploaded"  # DocumentStatus default applied


@pytest.mark.integration
async def test_composite_fk_rejects_cross_tenant_document_version(
    db_session: AsyncSession,
) -> None:
    """
    The critical test (Section 127 of the architecture brief, applied at the schema
    layer): a DocumentVersion whose tenant_id does not match its parent Document's
    tenant_id must be rejected by the DATABASE, not merely by application logic that
    could contain a bug.
    """
    tenant_a = await _make_tenant(db_session, "TenantA")
    tenant_b = await _make_tenant(db_session, "TenantB")

    doc = Document(
        tenant_id=tenant_a.id,
        source_type="upload",
        title="Confidential HR Policy",
        mime_type="application/pdf",
        content_hash="b" * 64,
    )
    db_session.add(doc)
    await db_session.flush()

    # Attempt to attach a version under tenant_b's tenant_id while pointing at
    # tenant_a's document_id -- this is exactly the bug class Section 127's
    # cross-tenant test targets, reproduced at the schema layer.
    malicious_version = DocumentVersion(
        tenant_id=tenant_b.id,
        document_id=doc.id,
        version=1,
        status="draft",
        content_hash="c" * 64,
    )
    db_session.add(malicious_version)

    with pytest.raises(IntegrityError, match="fk_document_versions_document"):
        await db_session.flush()


@pytest.mark.integration
async def test_check_constraint_rejects_invalid_document_status(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "StatusCheckCo")
    doc = Document(
        tenant_id=tenant.id,
        source_type="upload",
        title="Test Doc",
        mime_type="text/plain",
        content_hash="d" * 64,
    )
    db_session.add(doc)
    await db_session.flush()

    with pytest.raises(IntegrityError, match="ck_documents_status_valid"):
        await db_session.execute(
            text("UPDATE documents SET status = 'not_a_real_status' WHERE id = :id"),
            {"id": doc.id},
        )
        await db_session.flush()


@pytest.mark.integration
async def test_check_constraint_rejects_user_specific_acl_without_user(
    db_session: AsyncSession,
) -> None:
    """
    A document_acl row claiming visibility='user_specific' but naming no user would be
    a silent authorization bug (Section 11): it must be rejected outright.
    """
    tenant = await _make_tenant(db_session, "AclCheckCo")
    doc = Document(
        tenant_id=tenant.id,
        source_type="upload",
        title="Restricted Doc",
        mime_type="text/plain",
        content_hash="e" * 64,
    )
    db_session.add(doc)
    await db_session.flush()

    bad_acl = DocumentACL(
        tenant_id=tenant.id,
        document_id=doc.id,
        visibility="user_specific",
        allowed_user_id=None,  # invalid: user_specific requires an actual user
    )
    db_session.add(bad_acl)

    with pytest.raises(IntegrityError, match="ck_document_acl_grant_matches_visibility"):
        await db_session.flush()


@pytest.mark.integration
async def test_unique_email_scoped_per_tenant_not_global(db_session: AsyncSession) -> None:
    """Two different tenants may have a user with the same email (Section 9)."""
    from packages.database.models import User

    tenant_a = await _make_tenant(db_session, "SameEmailA")
    tenant_b = await _make_tenant(db_session, "SameEmailB")

    user_a = User(
        tenant_id=tenant_a.id,
        email="alice@example.com",
        hashed_password="x",
        display_name="Alice",
    )
    user_b = User(
        tenant_id=tenant_b.id,
        email="alice@example.com",
        hashed_password="x",
        display_name="Alice B",
    )
    db_session.add_all([user_a, user_b])
    await db_session.flush()  # must NOT raise -- this is the whole point of the test

    assert user_a.id != user_b.id
