"""Phase 34: how the API behaves when its dependencies fail or hang (in-process, deterministic).

The real-fault versions (service stop, table locks, black-holed sockets) are in
scripts/chaos_run.py and documented in docs/resilience/PHASE_34_RESILIENCE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from apps.api.app import main as main_module
from apps.api.app.dependencies.db import get_db_session
from apps.api.app.main import create_app
from httpx import ASGITransport, AsyncClient
from packages.config.settings import Settings, get_settings
from packages.database.session import get_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

pytestmark = pytest.mark.integration

LOGIN = {"tenant_slug": "x", "email": "a@example.com", "password": "p"}


def _app_with_db_failure(exc: BaseException) -> Any:
    async def broken() -> None:
        raise exc

    app = create_app()
    app.dependency_overrides[get_db_session] = broken
    return app


async def _post_login(app: Any) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        return await c.post("/api/v1/auth/login", json=LOGIN)


async def test_database_connection_refused_is_503_retryable() -> None:
    r = await _post_login(_app_with_db_failure(ConnectionRefusedError(111, "refused")))
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "refused" not in r.text  # exception text must not reach the client


async def test_sqlalchemy_operational_error_is_503() -> None:
    err = OperationalError("select 1", {}, Exception("secret-host:5432 password=hunter2"))
    r = await _post_login(_app_with_db_failure(err))
    assert r.status_code == 503
    assert "hunter2" not in r.text and "secret-host" not in r.text


async def test_timeout_is_504_retryable() -> None:
    r = await _post_login(_app_with_db_failure(TimeoutError()))
    assert r.status_code == 504
    assert r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "DEPENDENCY_TIMEOUT"


async def test_real_statement_timeout_is_504_not_500() -> None:
    """A genuine Postgres query_canceled (57014), produced by a real slow statement."""
    engine = get_engine(get_settings().database)

    async def slow_session() -> Any:
        async with engine.connect() as conn:
            await conn.execute(text("set statement_timeout = 100"))
            await conn.execute(text("select pg_sleep(2)"))
        yield None  # pragma: no cover

    app = create_app()
    app.dependency_overrides[get_db_session] = slow_session
    t0 = time.perf_counter()
    r = await _post_login(app)
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "DATABASE_TIMEOUT"
    assert time.perf_counter() - t0 < 1.5  # bounded by the timeout, not by pg_sleep


async def test_other_database_errors_remain_500() -> None:
    err = IntegrityError("insert", {}, Exception("duplicate key"))
    r = await _post_login(_app_with_db_failure(err))
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "duplicate" not in r.text


async def test_every_pooled_connection_has_a_statement_timeout() -> None:
    settings = get_settings()
    engine = get_engine(settings.database)
    async with engine.connect() as conn:
        value = (await conn.execute(text("show statement_timeout"))).scalar_one()
    assert value in {f"{settings.database.statement_timeout_ms // 1000}s", "15s"}


# ---- readiness --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _ready_app(_mp: pytest.MonkeyPatch, **patch: Any) -> AsyncIterator[Any]:
    """App whose settings are overridden AFTER startup (the lifespan assigns app.state.settings)."""
    app = create_app()
    async with app.router.lifespan_context(app):
        s: Settings = get_settings()
        health = s.health.model_copy(update=patch.get("health", {}))
        hardening = s.hardening.model_copy(update=patch.get("hardening", {}))
        app.state.settings = s.model_copy(update={"health": health, "hardening": hardening})
        yield app


async def _ready(app: Any) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        t0 = time.perf_counter()
        r = await c.get("/api/v1/health/ready")
        return r, time.perf_counter() - t0


async def _boom(*_a: Any, **_k: Any) -> None:
    raise ConnectionError("redis down")


async def _hang(*_a: Any, **_k: Any) -> None:
    await asyncio.sleep(30)


async def test_redis_down_is_degraded_but_ready_when_throttle_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("packages.redis.client.check_redis_connection", _boom)
    async with _ready_app(monkeypatch, hardening={"login_throttle_fail_open": True}) as app:
        r, _ = await _ready(app)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"].startswith("degraded:")


async def test_redis_down_fails_readiness_when_throttle_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("packages.redis.client.check_redis_connection", _boom)
    async with _ready_app(monkeypatch, hardening={"login_throttle_fail_open": False}) as app:
        r, _ = await _ready(app)
    assert r.status_code == 503
    assert r.json()["checks"]["redis"].startswith("unavailable:")


async def test_redis_required_can_be_forced_either_way(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.redis.client.check_redis_connection", _boom)
    async with _ready_app(monkeypatch, health={"redis_required": True}) as app:
        r, _ = await _ready(app)
    assert r.status_code == 503


async def test_database_down_always_fails_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.database.session.check_database_connection", _boom)
    async with _ready_app(monkeypatch) as app:
        r, _ = await _ready(app)
    assert r.status_code == 503
    assert r.json()["checks"]["database"].startswith("unavailable:")


async def test_hung_dependencies_are_bounded_and_checked_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("packages.database.session.check_database_connection", _hang)
    monkeypatch.setattr("packages.redis.client.check_redis_connection", _hang)
    patch = {"health": {"check_timeout_seconds": 0.3, "redis_required": True}}
    async with _ready_app(monkeypatch, **patch) as app:
        r, elapsed = await _ready(app)
    assert r.status_code == 503
    assert r.json()["checks"] == {
        "database": "unavailable: timeout",
        "redis": "unavailable: timeout",
        "kafka": "disabled",
    }
    assert elapsed < 0.8  # ~0.3 s (concurrent), not 0.6 s (sequential) and nowhere near 30 s


async def test_liveness_ignores_dependency_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.database.session.check_database_connection", _boom)
    monkeypatch.setattr("packages.redis.client.check_redis_connection", _boom)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/v1/health/live")).status_code == 200


def test_main_module_exposes_no_unhandled_dependency_gap() -> None:
    # Guard: the handlers this phase added must stay registered on the app.
    app = create_app()
    handlers = set(app.exception_handlers)
    for cls in (ConnectionError, TimeoutError, OperationalError):
        assert cls in handlers
    assert main_module.create_app is create_app
