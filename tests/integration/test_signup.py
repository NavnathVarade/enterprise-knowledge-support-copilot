"""Self-service signup (POST /api/v1/auth/signup) over the real app, Postgres and Redis."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from apps.api.app.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from packages.config.settings import get_settings
from packages.database.models import AuditEvent, Tenant, User
from packages.database.session import get_sessionmaker
from packages.redis.client import get_redis_client
from sqlalchemy import func, select

pytestmark = pytest.mark.integration

GOOD_PASSWORD = "correct-horse-battery-9"


def _body(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "organization_name": "Signup Test Org",
        "tenant_slug": f"su-{uuid.uuid4().hex[:10]}",
        "display_name": "Ada Admin",
        "email": "ada@example.com",
        "password": GOOD_PASSWORD,
    }
    base.update(over)
    return base


async def _reset_limit() -> None:
    redis = get_redis_client(get_settings().redis)
    async for key in redis.scan_iter("signup:attempt:ip:*"):
        await redis.delete(key)


@pytest.fixture
async def app_client() -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    await _reset_limit()
    app = create_app()
    async with app.router.lifespan_context(app):
        s = app.state.settings
        app.state.settings = s.model_copy(
            update={"signup": s.signup.model_copy(update={"enabled": True, "max_per_ip": 1000})}
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield app, c
    await _reset_limit()


async def test_signup_creates_tenant_admin_and_a_working_login(
    app_client: tuple[FastAPI, AsyncClient],
) -> None:
    _, client = app_client
    body = _body()
    resp = await client.post("/api/v1/auth/signup", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["tenant_slug"] == body["tenant_slug"]
    assert data["token_type"] == "bearer"

    # The returned token is a real session: it can call an admin-only endpoint.
    me = await client.get(
        "/api/v1/audit", headers={"Authorization": f"Bearer {data['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert any(e["action"] == "auth.signup" for e in me.json())

    # And the password works through the normal login path.
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "tenant_slug": body["tenant_slug"],
            "email": "ada@example.com",
            "password": GOOD_PASSWORD,
        },
    )
    assert login.status_code == 200, login.text


async def test_signup_never_stores_the_password(app_client: tuple[FastAPI, AsyncClient]) -> None:
    _, client = app_client
    body = _body()
    assert (await client.post("/api/v1/auth/signup", json=body)).status_code == 201
    async with get_sessionmaker(get_settings().database)() as db:
        user = (
            await db.execute(select(User).join(Tenant).where(Tenant.slug == body["tenant_slug"]))
        ).scalar_one()
        assert user.hashed_password != GOOD_PASSWORD
        assert user.hashed_password.startswith("$2")
        events = (
            (await db.execute(select(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id)))
            .scalars()
            .all()
        )
        assert GOOD_PASSWORD not in repr([e.details for e in events])


async def test_duplicate_slug_is_a_conflict_and_does_not_touch_the_existing_tenant(
    app_client: tuple[FastAPI, AsyncClient],
) -> None:
    _, client = app_client
    first = _body()
    assert (await client.post("/api/v1/auth/signup", json=first)).status_code == 201
    second = _body(
        tenant_slug=first["tenant_slug"], email="mallory@example.com", password="another-pass-77"
    )
    resp = await client.post("/api/v1/auth/signup", json=second)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TENANT_SLUG_TAKEN"
    async with get_sessionmaker(get_settings().database)() as db:
        n = (
            await db.execute(
                select(func.count())
                .select_from(User)
                .join(Tenant)
                .where(Tenant.slug == first["tenant_slug"])
            )
        ).scalar_one()
    assert n == 1


async def test_same_email_in_two_new_organizations_is_allowed(
    app_client: tuple[FastAPI, AsyncClient],
) -> None:
    _, client = app_client
    assert (await client.post("/api/v1/auth/signup", json=_body())).status_code == 201
    assert (await client.post("/api/v1/auth/signup", json=_body())).status_code == 201


@pytest.mark.parametrize(
    ("password", "email"),
    [
        ("short1", "ada@example.com"),
        ("onlyletterspassword", "ada@example.com"),
        ("1234567890123", "ada@example.com"),
        ("my-adalovelace-pass1", "adalovelace@example.com"),
        ("x1" * 40, "ada@example.com"),
    ],
)
async def test_weak_passwords_are_rejected(
    app_client: tuple[FastAPI, AsyncClient], password: str, email: str
) -> None:
    _, client = app_client
    resp = await client.post("/api/v1/auth/signup", json=_body(password=password, email=email))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "WEAK_PASSWORD"


@pytest.mark.parametrize(
    "bad",
    [
        {"tenant_slug": "UP"},
        {"tenant_slug": "-bad-"},
        {"tenant_slug": "has space"},
        {"email": "not-an-email"},
        {"organization_name": " "},
        {"display_name": ""},
    ],
)
async def test_invalid_fields_are_422(
    app_client: tuple[FastAPI, AsyncClient], bad: dict[str, str]
) -> None:
    _, client = app_client
    resp = await client.post("/api/v1/auth/signup", json=_body(**bad))
    assert resp.status_code == 422


async def test_signup_is_refused_when_disabled(app_client: tuple[FastAPI, AsyncClient]) -> None:
    app, client = app_client
    s = app.state.settings
    app.state.settings = s.model_copy(
        update={"signup": s.signup.model_copy(update={"enabled": False})}
    )
    body = _body()
    resp = await client.post("/api/v1/auth/signup", json=body)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SIGNUP_DISABLED"
    async with get_sessionmaker(get_settings().database)() as db:
        assert (
            await db.execute(select(Tenant).where(Tenant.slug == body["tenant_slug"]))
        ).first() is None


async def test_signup_is_throttled_per_ip_with_retry_after(
    app_client: tuple[FastAPI, AsyncClient],
) -> None:
    app, client = app_client
    s = app.state.settings
    app.state.settings = s.model_copy(
        update={"signup": s.signup.model_copy(update={"max_per_ip": 2})}
    )
    codes = [(await client.post("/api/v1/auth/signup", json=_body())).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
    last = await client.post("/api/v1/auth/signup", json=_body())
    assert last.status_code == 429
    assert last.json()["error"]["code"] == "SIGNUP_THROTTLED"
    assert int(last.headers["retry-after"]) > 0


def test_signup_default_depends_on_environment() -> None:
    from packages.config.settings import Environment

    base = get_settings()
    for env, expected in [
        (Environment.development, True),
        (Environment.test, True),
        (Environment.staging, False),
        (Environment.production, False),
    ]:
        # model_copy skips validation on purpose: only the derived switch is under test.
        assert base.model_copy(update={"environment": env}).signup_enabled is expected
    forced = base.model_copy(
        update={
            "environment": Environment.production,
            "signup": base.signup.model_copy(update={"enabled": True}),
        }
    )
    assert forced.signup_enabled is True
