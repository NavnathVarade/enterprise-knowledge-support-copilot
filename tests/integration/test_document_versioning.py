"""
Proves, against a real Postgres, the scenario Section 128 of the architecture brief
describes: a document with three published versions over time, where "what's the
current policy" and "what was the policy back then" must resolve to different rows --
and where the highest version number is deliberately NOT what "current" means once a
newer draft exists that hasn't been published yet.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from packages.config.settings import get_settings
from packages.database.models import Tenant
from packages.database.repositories.documents import (
    create_document,
    create_draft_version,
    get_current_version,
    get_version_by_number,
    publish_version,
    transition_document_status,
)
from packages.database.session import get_sessionmaker
from packages.domain.errors import IllegalStatusTransition, InvalidDocument
from packages.domain.hashing import sha256_hex_of_text
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
async def test_document_status_transition_persists_and_rejects_illegal_moves(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "LifecycleCo")
    doc = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Runbook",
        source_type="upload",
        mime_type="text/markdown",
        content_hash=sha256_hex_of_text("v1 content"),
    )
    assert doc.status == "uploaded"

    doc = await transition_document_status(
        db_session, tenant_id=tenant.id, document_id=doc.id, target_status="validating"
    )
    assert doc.status == "validating"

    with pytest.raises(IllegalStatusTransition):
        await transition_document_status(
            db_session, tenant_id=tenant.id, document_id=doc.id, target_status="active"
        )


@pytest.mark.integration
async def test_temporal_resolution_current_vs_historical_vs_comparison(
    db_session: AsyncSession,
) -> None:
    tenant = await _make_tenant(db_session, "PolicyCo")
    doc = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Refund Policy",
        source_type="upload",
        mime_type="text/markdown",
        content_hash=sha256_hex_of_text("refund policy v1"),
    )

    now = datetime.now(UTC)
    t0 = now - timedelta(days=60)
    t1 = now - timedelta(days=30)
    t2 = now - timedelta(days=1)

    v1 = await create_draft_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, content_hash=sha256_hex_of_text("v1")
    )
    v1 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=doc.id,
        version_number=v1.version,
        effective_from=t0,
    )

    v2 = await create_draft_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, content_hash=sha256_hex_of_text("v2")
    )
    v2 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=doc.id,
        version_number=v2.version,
        effective_from=t1,
    )

    v3 = await create_draft_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, content_hash=sha256_hex_of_text("v3")
    )
    v3 = await publish_version(
        db_session,
        tenant_id=tenant.id,
        document_id=doc.id,
        version_number=v3.version,
        effective_from=t2,
    )

    current = await get_current_version(db_session, tenant_id=tenant.id, document_id=doc.id)
    assert current is not None
    assert current.version == v3.version
    assert current.id == v3.id

    historical = await get_current_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, as_of=t0 + timedelta(days=1)
    )
    assert historical is not None
    assert historical.version == v1.version

    v1_reloaded = await get_version_by_number(
        db_session, tenant_id=tenant.id, document_id=doc.id, version_number=v1.version
    )
    assert v1_reloaded is not None
    assert v1_reloaded.status == "superseded"
    assert v1_reloaded.effective_until == t1

    v2_reloaded = await get_version_by_number(
        db_session, tenant_id=tenant.id, document_id=doc.id, version_number=v2.version
    )
    assert v2_reloaded is not None
    assert v2_reloaded.id != v3.id
    assert v2_reloaded.content_hash != v3.content_hash


@pytest.mark.integration
async def test_draft_version_is_never_returned_as_current(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "DraftLeakCo")
    doc = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Security Policy",
        source_type="upload",
        mime_type="text/markdown",
        content_hash=sha256_hex_of_text("v1"),
    )

    v1 = await create_draft_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, content_hash=sha256_hex_of_text("v1")
    )
    await publish_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, version_number=v1.version
    )

    await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=doc.id,
        content_hash=sha256_hex_of_text("unpublished draft"),
    )

    current = await get_current_version(db_session, tenant_id=tenant.id, document_id=doc.id)
    assert current is not None
    assert current.version == v1.version
    assert current.status == "published"


@pytest.mark.integration
async def test_cannot_publish_a_non_draft_version(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "DoublePublishCo")
    doc = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("x"),
    )
    v1 = await create_draft_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, content_hash=sha256_hex_of_text("x")
    )
    await publish_version(
        db_session, tenant_id=tenant.id, document_id=doc.id, version_number=v1.version
    )

    with pytest.raises(InvalidDocument):
        await publish_version(
            db_session, tenant_id=tenant.id, document_id=doc.id, version_number=v1.version
        )


@pytest.mark.integration
@pytest.mark.security
async def test_get_current_version_is_tenant_scoped(db_session: AsyncSession) -> None:
    tenant_a = await _make_tenant(db_session, "VersionTenantA")
    tenant_b = await _make_tenant(db_session, "VersionTenantB")

    doc = await create_document(
        db_session,
        tenant_id=tenant_a.id,
        title="Tenant A Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("a"),
    )
    v1 = await create_draft_version(
        db_session, tenant_id=tenant_a.id, document_id=doc.id, content_hash=sha256_hex_of_text("a")
    )
    await publish_version(
        db_session, tenant_id=tenant_a.id, document_id=doc.id, version_number=v1.version
    )

    result = await get_current_version(db_session, tenant_id=tenant_b.id, document_id=doc.id)
    assert result is None
