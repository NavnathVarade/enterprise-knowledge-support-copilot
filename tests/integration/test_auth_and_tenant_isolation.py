"""
End-to-end auth + tenant isolation tests, over real HTTP via the actual FastAPI app --
not just unit tests of the JWT/password logic in isolation. This is the closest thing
Phase 4 has to Section 127's "critical security E2E test": two tenants, one with data
the other must never see, exercised through the real request path (login -> token ->
authenticated request), not by calling repository functions directly.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from packages.config.settings import get_settings
from packages.database.models import Document, DocumentACL, Tenant, User
from packages.database.session import get_sessionmaker
from packages.security.passwords import hash_password
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def db_session() -> AsyncSession:
    settings = get_settings()
    sessionmaker = get_sessionmaker(settings.database)
    async with sessionmaker() as session:
        yield session
        await session.rollback()


async def _make_tenant_with_user_and_document(
    session: AsyncSession, *, name: str, password: str, doc_title: str
) -> tuple[Tenant, User]:
    tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()

    user = User(
        tenant_id=tenant.id,
        email="owner@example.com",
        hashed_password=hash_password(password),
        display_name="Owner",
    )
    session.add(user)

    doc = Document(
        tenant_id=tenant.id,
        source_type="upload",
        title=doc_title,
        mime_type="text/plain",
        content_hash="f" * 64,
    )
    session.add(doc)
    await session.flush()
    # As of Phase 16, ACL is fail-closed: a document with zero document_acl rows is
    # authorized to nobody, including its own tenant's users. This fixture predates
    # that and must now explicitly grant visibility for the document to appear in
    # GET /documents -- the fixture was wrong to assume implicit visibility, not the
    # ACL enforcement.
    session.add(
        DocumentACL(tenant_id=tenant.id, document_id=doc.id, visibility="public_within_tenant")
    )
    await session.commit()
    return tenant, user


@pytest.mark.integration
async def test_login_with_valid_credentials_succeeds(db_session: AsyncSession) -> None:
    from apps.api.app.main import create_app

    tenant, _ = await _make_tenant_with_user_and_document(
        db_session, name="LoginOk", password="hunter22", doc_title="doc"
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/auth/login",
                json={
                    "tenant_slug": tenant.slug,
                    "email": "owner@example.com",
                    "password": "hunter22",
                },
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


@pytest.mark.integration
async def test_login_with_wrong_password_is_rejected(db_session: AsyncSession) -> None:
    from apps.api.app.main import create_app

    tenant, _ = await _make_tenant_with_user_and_document(
        db_session, name="LoginBad", password="correctpassword", doc_title="doc"
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/auth/login",
                json={
                    "tenant_slug": tenant.slug,
                    "email": "owner@example.com",
                    "password": "wrongpassword",
                },
            )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.integration
async def test_documents_endpoint_requires_authentication() -> None:
    from apps.api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/documents")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "MISSING_CREDENTIALS"


@pytest.mark.integration
@pytest.mark.security
async def test_cross_tenant_document_isolation_over_real_http(db_session: AsyncSession) -> None:
    """
    THE critical test (Section 127): Tenant A has a confidential document. Tenant B's
    authenticated user must see ZERO of Tenant A's documents when calling the same
    endpoint -- exercised through real login + real bearer-token auth, not a direct
    repository call.
    """
    from apps.api.app.main import create_app

    tenant_a, _ = await _make_tenant_with_user_and_document(
        db_session,
        name="TenantAHttp",
        password="password-a",
        doc_title="Tenant A Confidential Salary Doc",
    )
    tenant_b, _ = await _make_tenant_with_user_and_document(
        db_session, name="TenantBHttp", password="password-b", doc_title="Tenant B Public FAQ"
    )

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            login_b = await client.post(
                "/api/v1/auth/login",
                json={
                    "tenant_slug": tenant_b.slug,
                    "email": "owner@example.com",
                    "password": "password-b",
                },
            )
            assert login_b.status_code == 200, login_b.text
            token_b = login_b.json()["access_token"]

            docs_resp = await client.get(
                "/api/v1/documents", headers={"Authorization": f"Bearer {token_b}"}
            )

    assert docs_resp.status_code == 200, docs_resp.text
    titles = [d["title"] for d in docs_resp.json()]
    assert titles == ["Tenant B Public FAQ"]
    assert "Tenant A Confidential Salary Doc" not in titles


@pytest.mark.integration
async def test_invalid_bearer_token_is_rejected() -> None:
    from apps.api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/documents", headers={"Authorization": "Bearer not-a-real-token"}
            )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_TOKEN"
