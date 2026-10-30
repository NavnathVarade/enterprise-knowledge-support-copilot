"""
Proves the seed script (scripts/seed_database.py) actually loads the synthetic dataset
correctly against a real Postgres, and that running it twice is a true no-op the second
time (Section 84's idempotency requirement, applied to seeding).
"""

from __future__ import annotations

import pytest
from data.synthetic.generator import generate_dataset
from packages.config.settings import get_settings
from packages.database.models import Document, DocumentACL, Tenant, User
from packages.database.repositories.identity import (
    get_role_names_for_user,
    get_user_by_tenant_and_email,
)
from packages.database.session import get_sessionmaker
from packages.security.passwords import verify_password
from scripts.seed_database import seed_database
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def db_session() -> AsyncSession:
    settings = get_settings()
    sessionmaker = get_sessionmaker(settings.database)
    async with sessionmaker() as session:
        yield session


async def _count(session: AsyncSession, model: type) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


@pytest.mark.integration
async def test_seeding_creates_expected_tenants_and_documents(db_session: AsyncSession) -> None:
    dataset = generate_dataset()
    await seed_database(dataset)

    result = await db_session.execute(select(Tenant).where(Tenant.slug == "acme-corp"))
    tenant = result.scalar_one()
    assert tenant.name == "Acme Corp"

    result = await db_session.execute(select(Document).where(Document.tenant_id == tenant.id))
    documents = result.scalars().all()
    titles = {d.title for d in documents}
    assert "Parental Leave Policy" in titles
    assert "Vendor Onboarding Notice" in titles
    assert len(documents) == 10  # matches the generator's document count


@pytest.mark.integration
async def test_seeded_user_can_authenticate_with_synthetic_password(
    db_session: AsyncSession,
) -> None:
    dataset = generate_dataset()
    await seed_database(dataset)

    result = await db_session.execute(select(Tenant).where(Tenant.slug == "acme-corp"))
    tenant = result.scalar_one()

    user = await get_user_by_tenant_and_email(
        db_session, tenant_id=tenant.id, email="alice@example.com"
    )
    assert user is not None
    assert verify_password("alice-synthetic-pw-1", user.hashed_password)

    roles = await get_role_names_for_user(db_session, user_id=user.id)
    assert roles == {"engineer"}


@pytest.mark.integration
async def test_restricted_documents_have_correct_acl_after_seeding(
    db_session: AsyncSession,
) -> None:
    dataset = generate_dataset()
    await seed_database(dataset)

    result = await db_session.execute(select(Tenant).where(Tenant.slug == "acme-corp"))
    tenant = result.scalar_one()

    result = await db_session.execute(
        select(Document).where(
            Document.tenant_id == tenant.id, Document.title == "Security Incident Response Policy"
        )
    )
    doc = result.scalar_one()
    assert doc.security_classification == "RESTRICTED"

    result = await db_session.execute(select(DocumentACL).where(DocumentACL.document_id == doc.id))
    acl_entries = result.scalars().all()
    assert len(acl_entries) == 1
    assert acl_entries[0].visibility == "role_based"
    assert acl_entries[0].allowed_role_id is not None


@pytest.mark.integration
async def test_stale_document_ends_up_archived(db_session: AsyncSession) -> None:
    dataset = generate_dataset()
    await seed_database(dataset)

    result = await db_session.execute(select(Tenant).where(Tenant.slug == "acme-corp"))
    tenant = result.scalar_one()

    result = await db_session.execute(
        select(Document).where(
            Document.tenant_id == tenant.id, Document.title == "Legacy Deployment Runbook"
        )
    )
    doc = result.scalar_one()
    assert doc.status == "archived"


@pytest.mark.integration
async def test_seeding_twice_is_idempotent(db_session: AsyncSession) -> None:
    dataset = generate_dataset()
    await seed_database(dataset)

    tenant_count_after_first = await _count(db_session, Tenant)
    user_count_after_first = await _count(db_session, User)
    document_count_after_first = await _count(db_session, Document)

    # Re-run the exact same seed.
    await seed_database(dataset)

    tenant_count_after_second = await _count(db_session, Tenant)
    user_count_after_second = await _count(db_session, User)
    document_count_after_second = await _count(db_session, Document)

    assert tenant_count_after_second == tenant_count_after_first
    assert user_count_after_second == user_count_after_first
    assert document_count_after_second == document_count_after_first
