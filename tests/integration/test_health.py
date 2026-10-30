from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.integration
async def test_liveness_never_touches_dependencies() -> None:
    from apps.api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


@pytest.mark.integration
async def test_readiness_reports_real_database_and_redis() -> None:
    from apps.api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/health/ready")
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


@pytest.mark.integration
async def test_correlation_ids_are_echoed() -> None:
    from apps.api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/health/live",
                headers={"x-request-id": "req-abc", "x-trace-id": "trace-xyz"},
            )
    assert resp.headers["x-request-id"] == "req-abc"
    assert resp.headers["x-trace-id"] == "trace-xyz"


@pytest.mark.integration
async def test_unhandled_exception_never_leaks_internals() -> None:
    from apps.api.app.main import create_app

    app = create_app()

    @app.get("/api/v1/_boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail: db password=hunter2")

    async with app.router.lifespan_context(app):
        # raise_app_exceptions=False: we want the actual HTTP response the framework
        # sent, not httpx's debug re-raise of the underlying exception (Starlette's
        # ServerErrorMiddleware always re-raises after sending its response so hosting
        # environments can log it -- that's expected framework behavior, not a bug here).
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/_boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in resp.text
    assert "db password" not in resp.text


@pytest.mark.integration
async def test_db_pool_exhaustion_is_503_with_retry_after_not_500() -> None:
    """Phase 33: under overload SQLAlchemy raises its pool TimeoutError; clients must get a
    retryable 503, not an opaque 500."""
    from apps.api.app.dependencies.db import get_db_session
    from apps.api.app.main import create_app
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    async def exhausted() -> None:
        raise PoolTimeoutError("QueuePool limit reached")

    app = create_app()
    app.dependency_overrides[get_db_session] = exhausted
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/auth/login",
            json={"tenant_slug": "x", "email": "a@example.com", "password": "p"},
        )
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "SERVICE_BUSY"
