"""
End-to-end tests for POST /api/v1/documents, over real HTTP with a real authenticated
user -- login, then upload, then confirm it shows up via GET /documents with the
correct status and tenant scoping.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from packages.config.settings import get_settings
from packages.database.models import Tenant, User
from packages.database.session import get_sessionmaker
from packages.security.passwords import hash_password
from packages.storage.object_store import InMemoryObjectStore
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def db_session() -> AsyncSession:
    settings = get_settings()
    sessionmaker = get_sessionmaker(settings.database)
    async with sessionmaker() as session:
        yield session
        await session.rollback()


async def _make_tenant_with_user(session: AsyncSession, *, name: str, password: str) -> Tenant:
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
    await session.commit()
    return tenant


async def _login(client: AsyncClient, tenant: Tenant, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": tenant.slug, "email": "owner@example.com", "password": password},
    )
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


@pytest.mark.integration
async def test_upload_returns_202_and_creates_uploaded_document(db_session: AsyncSession) -> None:
    from apps.api.app.main import create_app

    tenant = await _make_tenant_with_user(db_session, name="UploadOkCo", password="pw-upload-1")

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.object_store = InMemoryObjectStore()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _login(client, tenant, "pw-upload-1")
            resp = await client.post(
                "/api/v1/documents",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("policy.md", b"# Refund Policy\n\n30 days.", "text/markdown")},
            )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["title"] == "policy.md"
    assert body["status"] == "uploaded"
    assert len(body["content_hash"]) == 64


@pytest.mark.integration
async def test_uploaded_document_appears_in_list(db_session: AsyncSession) -> None:
    from apps.api.app.main import create_app

    tenant = await _make_tenant_with_user(db_session, name="UploadListCo", password="pw-upload-2")

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.object_store = InMemoryObjectStore()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _login(client, tenant, "pw-upload-2")
            await client.post(
                "/api/v1/documents",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("runbook.md", b"# Runbook", "text/markdown")},
            )
            resp = await client.get(
                "/api/v1/documents", headers={"Authorization": f"Bearer {token}"}
            )

    titles = [d["title"] for d in resp.json()]
    assert "runbook.md" in titles


@pytest.mark.integration
async def test_upload_rejects_disallowed_mime_type(db_session: AsyncSession) -> None:
    from apps.api.app.main import create_app

    tenant = await _make_tenant_with_user(db_session, name="UploadMimeCo", password="pw-upload-3")

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.object_store = InMemoryObjectStore()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _login(client, tenant, "pw-upload-3")
            resp = await client.post(
                "/api/v1/documents",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("virus.exe", b"MZ...", "application/x-executable")},
            )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNSUPPORTED_MIME_TYPE"


@pytest.mark.integration
async def test_upload_rejects_path_traversal_filename(db_session: AsyncSession) -> None:
    from apps.api.app.main import create_app

    tenant = await _make_tenant_with_user(db_session, name="UploadPathCo", password="pw-upload-4")

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.object_store = InMemoryObjectStore()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token = await _login(client, tenant, "pw-upload-4")
            resp = await client.post(
                "/api/v1/documents",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("../../etc/passwd", b"content", "text/plain")},
            )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_DOCUMENT"


@pytest.mark.integration
async def test_upload_requires_authentication() -> None:
    from apps.api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.object_store = InMemoryObjectStore()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/documents",
                files={"file": ("policy.md", b"content", "text/markdown")},
            )

    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.security
async def test_uploaded_document_is_tenant_scoped(db_session: AsyncSession) -> None:
    """A document uploaded by Tenant A's user must not appear in Tenant B's list."""
    from apps.api.app.main import create_app

    tenant_a = await _make_tenant_with_user(db_session, name="UploadTenantA", password="pw-a")
    tenant_b = await _make_tenant_with_user(db_session, name="UploadTenantB", password="pw-b")

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.object_store = InMemoryObjectStore()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            token_a = await _login(client, tenant_a, "pw-a")
            await client.post(
                "/api/v1/documents",
                headers={"Authorization": f"Bearer {token_a}"},
                files={"file": ("tenant-a-secret.md", b"secret", "text/markdown")},
            )

            token_b = await _login(client, tenant_b, "pw-b")
            resp_b = await client.get(
                "/api/v1/documents", headers={"Authorization": f"Bearer {token_b}"}
            )

    titles_b = [d["title"] for d in resp_b.json()]
    assert "tenant-a-secret.md" not in titles_b
