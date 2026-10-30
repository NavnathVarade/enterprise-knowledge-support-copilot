"""
Real-Postgres proof of the SQL analytics protections: builder correctness, tenant
isolation, row-level security, the read-only role (created from the shipped
infrastructure/sql script), read-only transactions, timeouts and truncation.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from packages.analytics.executor import AnalyticsExecutor
from packages.analytics.intent import Filter, GroupBy, Metric, QueryIntent
from packages.analytics.templates import (
    avg_first_response_by_service,
    open_incidents_by_service,
    tickets_by_status,
    tickets_created,
)
from packages.config.settings import get_settings
from packages.database.models import Incident, SupportTicket, Tenant
from packages.database.session import get_sessionmaker
from packages.domain.errors import AnalyticsQueryTimeout, InvalidAnalyticsQuery
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = pytest.mark.integration

RO_PASSWORD = "ro_test_password"
ROLE = "ekc_analytics_ro"
SQL_FILE = (
    pathlib.Path(__file__).parents[2] / "infrastructure" / "sql" / "analytics_readonly_role.sql"
)
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def ro_role() -> None:
    if shutil.which("psql") is None:
        pytest.skip("psql not available")
    base = str(get_settings().database.url).replace("postgresql+asyncpg", "postgresql")
    result = subprocess.run(
        [
            "psql",
            base,
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"ro_password={RO_PASSWORD}",
            "-f",
            str(SQL_FILE),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    # Allow the (superuser) test connection to SET ROLE to it.


@pytest.fixture
async def db() -> AsyncSession:
    async with get_sessionmaker(get_settings().database)() as session:
        yield session
        await session.rollback()


def _executor(**kw: Any) -> AnalyticsExecutor:
    return AnalyticsExecutor(get_sessionmaker(get_settings().database), role_name=ROLE, **kw)


async def _tenant(db: AsyncSession) -> uuid.UUID:
    t = Tenant(name="An", slug=f"an-{uuid.uuid4().hex[:8]}")
    db.add(t)
    await db.flush()
    return t.id


def _ticket(
    tenant: uuid.UUID, status: str, days_ago: int, service: str = "payments", frs: int | None = 60
) -> SupportTicket:
    return SupportTicket(
        tenant_id=tenant,
        service_name=service,
        status=status,
        priority="high",
        channel="email",
        opened_at=NOW - timedelta(days=days_ago),
        first_response_seconds=frs,
    )


def _incident(
    tenant: uuid.UUID, service: str, status: str, sev: str = "SEV2", n: int = 10
) -> Incident:
    return Incident(
        tenant_id=tenant,
        service_name=service,
        severity=sev,
        status=status,
        affected_customer_count=n,
        started_at=NOW - timedelta(days=2),
    )


@pytest.fixture
async def seeded(db: AsyncSession, ro_role: None) -> tuple[uuid.UUID, uuid.UUID]:
    a, b = await _tenant(db), await _tenant(db)
    db.add_all(
        [
            _ticket(a, "open", 1),
            _ticket(a, "open", 2, frs=120),
            _ticket(a, "resolved", 3, service="billing", frs=30),
            _ticket(a, "resolved", 40),  # outside a 30-day window
            _incident(a, "payments", "open"),
            _incident(a, "payments", "investigating"),
            _incident(a, "billing", "open"),
            _incident(a, "billing", "resolved"),
            _ticket(b, "open", 1),
            _ticket(b, "open", 1),
            _ticket(b, "open", 1),
            _incident(b, "payments", "open", n=999),
        ]
    )
    await db.commit()
    return a, b


async def test_count_in_range_excludes_outside_and_other_tenants(
    seeded: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, b = seeded
    r = await _executor().execute(tickets_created(NOW - timedelta(days=30), NOW), a)
    assert r.rows == [{"value": 3}]  # not 4 (old one), not 7 (tenant b)
    r_b = await _executor().execute(tickets_created(NOW - timedelta(days=30), NOW), b)
    assert r_b.rows == [{"value": 3}]


async def test_group_by_status(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = seeded
    r = await _executor().execute(tickets_by_status(NOW - timedelta(days=365), NOW), a)
    assert {row["status"]: row["value"] for row in r.rows} == {"open": 2, "resolved": 2}


async def test_open_incidents_by_service_template(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = seeded
    r = await _executor().execute(open_incidents_by_service(), a)
    assert {row["service_name"]: row["value"] for row in r.rows} == {"payments": 2, "billing": 1}
    assert r.rows[0]["service_name"] == "payments"  # metric_desc ordering


async def test_avg_ignores_nulls_and_is_json_safe(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = seeded
    r = await _executor().execute(avg_first_response_by_service(NOW - timedelta(days=365), NOW), a)
    by = {row["service_name"]: row["value"] for row in r.rows}
    assert by["billing"] == 30.0 and isinstance(by["payments"], float)


async def test_sum_and_distinct_metrics(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, b = seeded
    total = await _executor().execute(
        QueryIntent("incidents", Metric("sum", "affected_customer_count")), a
    )
    assert total.rows == [{"value": 40}]  # 4 incidents x 10, tenant b's 999 excluded
    distinct = await _executor().execute(
        QueryIntent("incidents", Metric("count_distinct", "service_name")), a
    )
    assert distinct.rows == [{"value": 2}]


async def test_time_bucketing(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = seeded
    intent = QueryIntent(
        "tickets",
        group_by=(GroupBy("opened_at", "day"),),
        time_start=NOW - timedelta(days=5),
        time_end=NOW,
    )
    r = await _executor().execute(intent, a)
    assert r.columns == ["opened_at_day", "value"] and len(r.rows) == 3
    assert all(isinstance(row["opened_at_day"], str) for row in r.rows)


async def test_truncation_flagged(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = seeded
    r = await _executor().execute(
        QueryIntent("tickets", group_by=(GroupBy("opened_at", "day"),), limit=2), a
    )
    assert r.truncated and r.row_count == 2
    full = await _executor().execute(
        QueryIntent("tickets", group_by=(GroupBy("opened_at", "day"),), limit=50), a
    )
    assert not full.truncated


async def test_sql_injection_attempt_is_inert(
    seeded: tuple[uuid.UUID, uuid.UUID], db: AsyncSession
) -> None:
    a, _ = seeded
    attack = "email' OR '1'='1'; DROP TABLE support_tickets; --"
    r = await _executor().execute(
        QueryIntent("tickets", filters=(Filter("channel", "eq", attack),)), a
    )
    assert r.rows == [{"value": 0}]
    assert (await db.execute(select(func.count()).select_from(SupportTicket))).scalar_one() > 0


async def test_invalid_intent_never_reaches_database(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = seeded
    with pytest.raises(InvalidAnalyticsQuery):
        await _executor().execute(QueryIntent("tickets", filters=(Filter("nope", "eq", "x"),)), a)


# ------------------------------------------------------- defense in depth: RLS + role
async def test_rls_isolates_even_without_a_tenant_filter(
    seeded: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A deliberately unfiltered statement still only sees the current tenant's rows."""
    a, b = seeded
    ex = _executor()
    stmt = select(func.count().label("n")).select_from(Incident)  # NO tenant_id predicate
    ra = await ex._run(stmt, a, limit=10)
    rb = await ex._run(stmt, b, limit=10)
    assert ra.rows[0]["n"] == 4 and rb.rows[0]["n"] == 1


async def test_rls_fails_closed_when_tenant_unset(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    engine = create_async_engine(
        str(get_settings().database.url).replace("//postgres:postgres@", f"//{ROLE}:{RO_PASSWORD}@")
    )
    try:
        async with engine.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM incidents"))).scalar_one()
        assert n == 0
    finally:
        await engine.dispose()


async def test_read_only_role_login_cannot_write_or_read_other_tables(
    seeded: tuple[uuid.UUID, uuid.UUID],
) -> None:
    a, _ = seeded
    engine = create_async_engine(
        str(get_settings().database.url).replace("//postgres:postgres@", f"//{ROLE}:{RO_PASSWORD}@")
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :t, false)"), {"t": str(a)}
            )
            assert (await conn.execute(text("SELECT count(*) FROM incidents"))).scalar_one() == 4
            for statement in (
                "INSERT INTO incidents (id, tenant_id, service_name, severity, status, affected_customer_count, started_at, created_at, updated_at) "
                f"VALUES (gen_random_uuid(), '{a}', 'x', 'SEV1', 'open', 1, now(), now(), now())",
                "UPDATE incidents SET status = 'resolved'",
                "DELETE FROM incidents",
                "DROP TABLE incidents",
                "SELECT count(*) FROM documents",
                "SELECT count(*) FROM users",
                "SELECT count(*) FROM embeddings",
            ):
                await conn.rollback()
                with pytest.raises(DBAPIError):
                    await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def test_read_only_transaction_blocks_writes_for_any_role(
    seeded: tuple[uuid.UUID, uuid.UUID], db: AsyncSession
) -> None:
    async with get_sessionmaker(get_settings().database)() as s, s.begin():
        await s.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(DBAPIError):
            await s.execute(text("DELETE FROM incidents"))


async def test_statement_timeout_maps_to_domain_error(seeded: tuple[uuid.UUID, uuid.UUID]) -> None:
    a, _ = seeded
    ex = _executor(statement_timeout_ms=100)
    with pytest.raises(AnalyticsQueryTimeout):
        await ex._run(select(func.pg_sleep(2)), a, limit=1)


def test_executor_rejects_bad_config() -> None:
    sm = get_sessionmaker(get_settings().database)
    with pytest.raises(ValueError):
        AnalyticsExecutor(sm, role_name='x"; DROP TABLE y; --')
    with pytest.raises(ValueError):
        AnalyticsExecutor(sm, statement_timeout_ms=0)
