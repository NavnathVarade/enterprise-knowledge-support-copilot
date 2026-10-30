"""
Database engine/session management.

Design notes:
  - One AsyncEngine per process, created lazily and cached — never re-created per request.
  - Every session obtained through `tenant_scoped_session` is bound to a tenant_id that
    the caller must supply from *authenticated context*, never from request body (Section 9).
    Repositories built on top of this session are expected to include `tenant_id` in every
    WHERE clause; this module does not silently enforce that at the SQL level by itself —
    row-level security (Postgres RLS) is documented as a defense-in-depth addition in
    Phase 3/16 and is not yet implemented here, so it must not be claimed as already active.
  - `check_database_connection` is intentionally cheap (SELECT 1) and bounded by
    statement_timeout so a readiness probe never hangs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.config.settings import DatabaseSettings


@lru_cache
def _engine_for(
    url: str,
    pool_size: int,
    max_overflow: int,
    pool_timeout: float,
    connect_timeout: float,
    statement_timeout_ms: int,
) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_pre_ping=True,
        future=True,
        # asyncpg prepares statements; after 5 executions Postgres may switch to a *generic*
        # plan that cannot see the tenant_id literal. Measured on the retrieval queries
        # (Phase 33): 20-70x slower (vector leg 26 ms -> 583 ms, lexical 12 ms -> 830 ms at
        # 10K chunks). Per-tenant selectivity is the whole point of these queries, so always
        # plan with the real parameter values.
        connect_args={
            # asyncpg's connect timeout defaults to 60 s; a black-holed database would otherwise
            # hold every request (and readiness probe) for that long (Phase 34).
            "timeout": connect_timeout,
            # Client-side backstop for a peer that stops answering without closing the socket.
            # The server-side statement_timeout below normally fires first.
            "command_timeout": statement_timeout_ms / 1000 + 5,
            "server_settings": {
                "plan_cache_mode": "force_custom_plan",
                # Applies to EVERY session, including unscoped ones (login) that never went
                # through tenant_scoped_session. A table lock now costs a bounded wait.
                "statement_timeout": str(statement_timeout_ms),
            },
        },
    )


def get_engine(settings: DatabaseSettings) -> AsyncEngine:
    return _engine_for(
        str(settings.url),
        settings.pool_size,
        settings.max_overflow,
        settings.pool_timeout_seconds,
        settings.connect_timeout_seconds,
        settings.statement_timeout_ms,
    )


def get_sessionmaker(settings: DatabaseSettings) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=get_engine(settings), expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def tenant_scoped_session(
    settings: DatabaseSettings, *, tenant_id: str
) -> AsyncIterator[AsyncSession]:
    """
    Yields a session with the statement timeout applied and the tenant_id bound into
    the session's application_name / a session-local GUC, so that even a query written
    without an explicit tenant filter can be audited from Postgres logs. This is a
    logging/defense-in-depth aid, NOT a substitute for explicit tenant_id filters in
    every repository query — those are still required.
    """
    sessionmaker = get_sessionmaker(settings)
    async with sessionmaker() as session:
        await session.execute(
            text(f"SET LOCAL statement_timeout = {settings.statement_timeout_ms}")
        )
        # NOTE: `SET LOCAL x = :param` is a Postgres syntax error -- SET/SET LOCAL only
        # accept a literal or identifier in that position, never a bind parameter.
        # set_config(setting_name, new_value, is_local) is the parameterized equivalent
        # that actually accepts a bound value safely. Caught by
        # tests/integration/test_auth_and_tenant_isolation.py::
        # test_cross_tenant_document_isolation_over_real_http -- the first test to
        # exercise this function via a real authenticated request rather than calling
        # it directly, which is why it went unnoticed since Phase 1.
        await session.execute(
            text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
        yield session


async def check_database_connection(settings: DatabaseSettings) -> None:
    engine = get_engine(settings)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
