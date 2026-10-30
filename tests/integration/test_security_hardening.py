"""Phase 32: login throttling, timing equalisation, headers, size limits, docs gating, seed guard.

Real HTTP, real Postgres, real Redis. Each test uses unique accounts and a unique
X-Forwarded-For (with one trusted hop) so Redis counters never collide across tests or runs.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from apps.api.app import main as main_module
from apps.api.app.main import create_app
from apps.api.app.routes import auth as auth_module
from httpx import ASGITransport, AsyncClient
from packages.config.settings import Environment, Settings, get_settings
from packages.database.models import Tenant, User
from packages.database.session import get_sessionmaker
from packages.security.passwords import hash_password
from redis.exceptions import ConnectionError as RedisConnectionError
from scripts.seed_database import SeedingRefused, seed_database
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PW = "correct-horse-battery"


@pytest.fixture
async def db() -> AsyncSession:
    async with get_sessionmaker(get_settings().database)() as session:
        yield session
        await session.rollback()


class Account:
    def __init__(self, slug: str, email: str) -> None:
        self.slug, self.email = slug, email


async def _account(db: AsyncSession) -> Account:
    t = Tenant(name="Sec", slug=f"sec-{uuid.uuid4().hex[:8]}")
    db.add(t)
    await db.flush()
    slug, tid = t.slug, t.id
    email = f"u{uuid.uuid4().hex[:6]}@example.com"
    db.add(User(tenant_id=tid, email=email, hashed_password=hash_password(PW), display_name="U"))
    await db.commit()
    return Account(slug, email)


def _settings(**hardening: Any) -> Settings:
    base = get_settings()
    return base.model_copy(
        update={
            "hardening": base.hardening.model_copy(
                update={"trusted_proxy_hops": 1, "login_throttle_fail_open": True, **hardening}
            )
        }
    )


async def _login(c: AsyncClient, a: Account, password: str, ip: str) -> Any:
    return await c.post(
        "/api/v1/auth/login",
        json={"tenant_slug": a.slug, "email": a.email, "password": password},
        headers={"X-Forwarded-For": ip},
    )


def _ip() -> str:
    return f"198.51.100.{uuid.uuid4().int % 250 + 1}.{uuid.uuid4().hex[:6]}"[:45]


async def _client(app: Any, settings: Settings | None = None) -> AsyncClient:
    app.state.settings = settings or _settings()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ------------------------------------------------------------------ login throttle


async def test_account_is_locked_after_repeated_failures_even_for_the_right_password(
    db: AsyncSession,
) -> None:
    a = await _account(db)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        await _client(app, _settings(login_max_failures_per_account=3)) as c,
    ):
        codes = [(await _login(c, a, "wrong", _ip())).status_code for _ in range(3)]
        assert codes == [401, 401, 401]
        locked = await _login(c, a, PW, _ip())  # correct password, different IP
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "LOGIN_THROTTLED"


async def test_success_resets_the_account_counter(db: AsyncSession) -> None:
    a = await _account(db)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        await _client(app, _settings(login_max_failures_per_account=3)) as c,
    ):
        for _ in range(2):
            assert (await _login(c, a, "wrong", _ip())).status_code == 401
        assert (await _login(c, a, PW, _ip())).status_code == 200
        for _ in range(2):  # would have been the 3rd and 4th failure without the reset
            assert (await _login(c, a, "wrong", _ip())).status_code == 401


async def test_throttling_does_not_reveal_whether_an_account_exists(db: AsyncSession) -> None:
    real = await _account(db)
    ghost = Account(real.slug, f"ghost{uuid.uuid4().hex[:6]}@example.com")
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        await _client(app, _settings(login_max_failures_per_account=3)) as c,
    ):
        seq_real = [(await _login(c, real, "x", _ip())).status_code for _ in range(5)]
        seq_ghost = [(await _login(c, ghost, "x", _ip())).status_code for _ in range(5)]
    assert seq_real == seq_ghost == [401, 401, 401, 429, 429]


async def test_one_ip_spraying_many_accounts_is_throttled(db: AsyncSession) -> None:
    ip = _ip()
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        await _client(app, _settings(login_max_failures_per_ip=4)) as c,
    ):
        codes = []
        for _ in range(6):
            a = Account(f"nope-{uuid.uuid4().hex[:6]}", f"x{uuid.uuid4().hex[:5]}@example.com")
            codes.append((await _login(c, a, "x", ip)).status_code)
        other = await _login(c, await _account(db), PW, _ip())
    assert codes == [401, 401, 401, 401, 429, 429]
    assert other.status_code == 200  # a different client is unaffected


async def test_a_spoofed_forwarded_for_cannot_dodge_the_ip_limit(db: AsyncSession) -> None:
    """With ZERO trusted hops X-Forwarded-For is ignored: rotating it does not reset the count."""
    app = create_app()
    async with app.router.lifespan_context(app):
        s = _settings(login_max_failures_per_ip=3, trusted_proxy_hops=0)
        async with await _client(app, s) as c:
            # All requests share the TCP peer; clear its counter first to stay repeatable.
            from packages.redis.client import get_redis_client
            from packages.security.login_throttle import ip_key

            await get_redis_client(s.redis).delete(ip_key("127.0.0.1"))
            codes = []
            for _ in range(5):
                a = Account(f"nope-{uuid.uuid4().hex[:6]}", f"x{uuid.uuid4().hex[:5]}@example.com")
                codes.append((await _login(c, a, "x", _ip())).status_code)
            await get_redis_client(s.redis).delete(ip_key("127.0.0.1"))
    assert codes == [401, 401, 401, 429, 429]


class _BrokenRedis:
    async def mget(self, *a: Any, **k: Any) -> Any:
        raise RedisConnectionError("down")

    async def incr(self, *a: Any, **k: Any) -> Any:
        raise RedisConnectionError("down")

    async def expire(self, *a: Any, **k: Any) -> Any:
        raise RedisConnectionError("down")

    async def delete(self, *a: Any, **k: Any) -> Any:
        raise RedisConnectionError("down")


async def test_redis_outage_follows_the_fail_open_setting(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = await _account(db)
    monkeypatch.setattr(auth_module, "get_redis_client", lambda settings: _BrokenRedis())
    app = create_app()
    async with app.router.lifespan_context(app):
        async with await _client(app, _settings(login_throttle_fail_open=True)) as c:
            assert (await _login(c, a, PW, _ip())).status_code == 200
        async with await _client(app, _settings(login_throttle_fail_open=False)) as c:
            assert (await _login(c, a, PW, _ip())).status_code == 429


# ------------------------------------------------------------------ timing / enumeration


async def test_every_failure_path_spends_one_bcrypt_check(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown tenant, unknown user, bad password and over-long password all do exactly one
    bcrypt verification, so response time does not reveal which one it was."""
    a = await _account(db)
    calls = {"n": 0}
    real_verify = auth_module.verify_password

    def counting(plain: str, hashed: str) -> bool:
        calls["n"] += 1
        return real_verify(plain, hashed)

    monkeypatch.setattr(auth_module, "verify_password", counting)
    long_pw = "p" * 100
    cases = [
        Account("no-such-tenant", a.email),  # unknown tenant
        Account(a.slug, f"ghost{uuid.uuid4().hex[:5]}@example.com"),  # unknown user
    ]
    app = create_app()
    async with app.router.lifespan_context(app), await _client(app) as c:
        for acct in cases:
            calls["n"] = 0
            r = await _login(c, acct, "whatever", _ip())
            assert r.status_code == 401 and calls["n"] == 1
        calls["n"] = 0
        assert (await _login(c, a, "wrong-password", _ip())).status_code == 401
        assert calls["n"] == 1
        calls["n"] = 0
        assert (await _login(c, a, long_pw, _ip())).status_code == 401  # not 422
        assert calls["n"] == 1


async def test_overlong_password_is_401_for_known_and_unknown_accounts_alike(
    db: AsyncSession,
) -> None:
    a = await _account(db)
    ghost = Account(a.slug, f"ghost{uuid.uuid4().hex[:5]}@example.com")
    app = create_app()
    async with app.router.lifespan_context(app), await _client(app) as c:
        known = await _login(c, a, "p" * 100, _ip())
        unknown = await _login(c, ghost, "p" * 100, _ip())
    assert known.status_code == unknown.status_code == 401
    assert known.json()["error"]["code"] == unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"


# ------------------------------------------------------------------ headers and ids


async def test_security_headers_on_success_error_and_not_found() -> None:
    app = create_app()
    async with app.router.lifespan_context(app), await _client(app) as c:
        for r in (
            await c.get("/api/v1/health/live"),
            await c.get("/api/v1/audit"),  # 401
            await c.get("/no/such/route"),  # 404
        ):
            assert r.headers["x-content-type-options"] == "nosniff", r.status_code
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_hsts_is_sent_in_production_only() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        prod = _settings().model_copy(update={"environment": Environment.production})
        async with await _client(app, prod) as c:
            assert (
                "max-age="
                in (await c.get("/api/v1/health/live")).headers["strict-transport-security"]
            )
        async with await _client(app) as c:
            assert "strict-transport-security" not in (await c.get("/api/v1/health/live")).headers


async def test_untrusted_request_ids_are_replaced_and_clean_ones_echoed() -> None:
    app = create_app()
    async with app.router.lifespan_context(app), await _client(app) as c:
        ok = await c.get("/api/v1/health/live", headers={"X-Request-Id": "req-abc-123"})
        assert ok.headers["x-request-id"] == "req-abc-123"
        long = await c.get("/api/v1/health/live", headers={"X-Request-Id": "x" * 300})
        assert uuid.UUID(long.headers["x-request-id"])
        spaced = await c.get("/api/v1/health/live", headers={"X-Request-Id": "a b; DROP"})
        assert uuid.UUID(spaced.headers["x-request-id"])


# ------------------------------------------------------------------ size limits


async def test_oversized_json_body_is_refused_with_413_and_headers() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        s = _settings()
        s = s.model_copy(
            update={"hardening": s.hardening.model_copy(update={"max_request_body_bytes": 2048})}
        )
        async with await _client(app, s) as c:
            r = await c.post("/api/v1/query", json={"query": "x" * 5000})
    assert r.status_code == 413 and r.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert r.headers["x-content-type-options"] == "nosniff" and "x-request-id" in r.headers


async def test_chunked_oversized_body_is_refused() -> None:
    app = create_app()

    async def chunks() -> Any:
        for _ in range(10):
            yield b"x" * 400

    async with app.router.lifespan_context(app):
        s = _settings()
        s = s.model_copy(
            update={"hardening": s.hardening.model_copy(update={"max_request_body_bytes": 1000})}
        )
        async with await _client(app, s) as c:
            r = await c.post(
                "/api/v1/query", content=chunks(), headers={"Content-Type": "application/json"}
            )
    assert r.status_code == 413


async def test_oversized_upload_is_refused_before_it_is_buffered() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        s = _settings()
        s = s.model_copy(
            update={
                "ingestion": s.ingestion.model_copy(update={"max_upload_size_bytes": 1000}),
                "hardening": s.hardening.model_copy(update={"upload_overhead_bytes": 500}),
            }
        )
        async with await _client(app, s) as c:
            r = await c.post(
                "/api/v1/documents", files={"file": ("a.md", b"x" * 4000, "text/markdown")}
            )
    assert r.status_code == 413  # before auth: unauthenticated callers cannot make us buffer


# ------------------------------------------------------------------ docs gating, seeding


@pytest.mark.parametrize(
    ("env", "flag", "expected"),
    [
        (Environment.production, None, 404),
        (Environment.staging, None, 404),
        (Environment.production, True, 200),
        (Environment.test, None, 200),
        (Environment.test, False, 404),
    ],
)
async def test_api_docs_are_gated_by_environment(
    monkeypatch: pytest.MonkeyPatch, env: Environment, flag: bool | None, expected: int
) -> None:
    base = get_settings()
    patched = base.model_copy(
        update={
            "environment": env,
            "hardening": base.hardening.model_copy(update={"docs_enabled": flag}),
        }
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: patched)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/openapi.json")).status_code == expected
        assert (await c.get("/docs")).status_code == expected
        assert (await c.get("/redoc")).status_code == expected


@pytest.mark.parametrize("env", [Environment.production, Environment.staging])
async def test_synthetic_seeding_is_refused_outside_dev_and_test(
    monkeypatch: pytest.MonkeyPatch, env: Environment
) -> None:
    import scripts.seed_database as seed_module

    base = get_settings()
    monkeypatch.setattr(
        seed_module, "get_settings", lambda: base.model_copy(update={"environment": env})
    )
    with pytest.raises(SeedingRefused, match="known passwords"):
        await seed_database()


# ------------------------------------------------------------------ upload hardening, end to end


async def _token_for(db: AsyncSession) -> tuple[Account, str]:
    a = await _account(db)
    app_login = create_app()
    async with app_login.router.lifespan_context(app_login), await _client(app_login) as c:
        r = await _login(c, a, PW, _ip())
    return a, r.json()["access_token"]


async def test_upload_route_rejects_a_corrupt_pdf_and_a_disguised_file(db: AsyncSession) -> None:
    from packages.storage.object_store import InMemoryObjectStore

    _, token = await _token_for(db)
    app = create_app()
    async with app.router.lifespan_context(app), await _client(app) as c:
        app.state.object_store = InMemoryObjectStore()
        h = {"Authorization": f"Bearer {token}"}
        corrupt = await c.post(
            "/api/v1/documents",
            headers=h,
            files={"file": ("x.pdf", b"%PDF-1.7 not really", "application/pdf")},
        )
        disguised = await c.post(
            "/api/v1/documents",
            headers=h,
            files={"file": ("x.txt", b"%PDF-1.7 something", "text/plain")},
        )
    assert corrupt.status_code == 422 and corrupt.json()["error"]["code"] == "INVALID_DOCUMENT"
    assert disguised.status_code == 422


async def test_pipeline_enforces_the_extracted_text_cap_and_announces_the_failure(
    db: AsyncSession,
) -> None:
    from packages.domain.errors import PayloadTooLarge
    from packages.ingestion.pipeline import handle_document_ingested
    from packages.kafka.topics import Topics
    from packages.storage.object_store import InMemoryObjectStore
    from tests.integration.test_ingestion_pipeline import (
        _deps,
        _doc,
        _ingested_event,
        _outbox,
        _upload,
    )

    store = InMemoryObjectStore()
    up = await _upload(db, store)
    sm = get_sessionmaker(get_settings().database)
    deps = _deps(sm, store)
    deps = type(deps)(
        **{
            **deps.__dict__,
            "ingestion_settings": deps.ingestion_settings.model_copy(
                update={"max_extracted_chars": 20}
            ),
        }
    )
    with pytest.raises(PayloadTooLarge):
        await handle_document_ingested(deps, await _ingested_event(db, up))
    assert (await _doc(db, up)).status == "processing_failed"
    (failed,) = await _outbox(db, up.tenant, Topics.DOCUMENT_PROCESSING_FAILED)
    assert failed.envelope["payload"]["error_type"] == "PayloadTooLarge"


async def test_login_does_not_block_the_event_loop(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 33 finding: bcrypt (~300 ms CPU) ran inline and stalled every other request on
    the worker. With the slow verify simulated by a blocking sleep, a concurrent ticker must
    keep running while the login is in flight."""
    import asyncio
    import time

    a = await _account(db)

    def slow_verify(_plain: str, _hashed: str) -> bool:
        time.sleep(0.4)  # blocking, like bcrypt; harmless only if it runs off the loop
        return False

    monkeypatch.setattr(auth_module, "verify_password", slow_verify)
    app = create_app()
    max_gap = 0.0
    async with app.router.lifespan_context(app), await _client(app) as c:
        done = asyncio.Event()

        async def ticker() -> None:
            nonlocal max_gap
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                max_gap = max(max_gap, now - last)
                last = now

        t = asyncio.create_task(ticker())
        r = await _login(c, a, "wrong-password", _ip())
        done.set()
        await t
    assert r.status_code == 401
    assert max_gap < 0.2, f"event loop was blocked for {max_gap:.3f}s during login"
