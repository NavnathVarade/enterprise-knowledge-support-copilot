"""
Analytics executor (Spec §39-40). Runs a built statement under every protection that
does not depend on the builder being bug-free:

  1. READ ONLY transaction (`SET TRANSACTION READ ONLY`).
  2. Per-transaction statement_timeout.
  3. `app.current_tenant_id` set so Postgres row-level security (migration 0004) limits
     rows to the tenant even if a statement lacked a tenant filter.
  4. In production the connection is the dedicated read-only role
     (`DATABASE_READ_ONLY_ROLE_URL`): SELECT on the analytics tables only.
     Where only the main DSN is available, `role_name` switches to that role with
     `SET LOCAL ROLE` for the transaction (needs membership; used by the test harness).
Rows are capped; `truncated` is reported rather than silently hiding the cut.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.analytics.builder import build_statement, effective_limit
from packages.analytics.intent import QueryIntent
from packages.domain.errors import AnalyticsQueryTimeout
from packages.observability.logging import get_logger

_log = get_logger(component="analytics_executor")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class AnalyticsResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    row_count: int
    duration_ms: float
    sql: str  # parameterized SQL text, for audit; contains no values


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


class AnalyticsExecutor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        statement_timeout_ms: int = 5000,
        role_name: str | None = None,
    ) -> None:
        if role_name is not None and not _IDENT.match(role_name):
            raise ValueError("role_name must be a plain lowercase identifier")
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        self._sessionmaker = sessionmaker
        self._timeout_ms = statement_timeout_ms
        self._role = role_name

    async def execute(self, intent: QueryIntent, tenant_id: uuid.UUID) -> AnalyticsResult:
        stmt = build_statement(intent, tenant_id)  # validation happens here, before any I/O
        return await self._run(stmt, tenant_id, limit=effective_limit(intent))

    async def _run(self, stmt: Select[Any], tenant_id: uuid.UUID, *, limit: int) -> AnalyticsResult:
        started = time.perf_counter()
        sql_text = str(stmt.compile())
        try:
            async with self._sessionmaker() as session, session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                if self._role is not None:
                    await session.execute(text(f'SET LOCAL ROLE "{self._role}"'))
                await session.execute(
                    text("SELECT set_config('app.current_tenant_id', :t, true)"),
                    {"t": str(tenant_id)},
                )
                await session.execute(
                    text("SELECT set_config('statement_timeout', :ms, true)"),
                    {"ms": str(self._timeout_ms)},
                )
                result = await session.execute(stmt)
                columns = list(result.keys())
                fetched = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
        except DBAPIError as exc:
            if (
                "statement timeout" in str(exc.orig).lower()
                or "QueryCanceled" in type(exc.orig).__name__
            ):
                _log.warning(
                    "analytics_timeout", tenant_id=str(tenant_id), timeout_ms=self._timeout_ms
                )
                raise AnalyticsQueryTimeout() from exc
            raise
        truncated = len(fetched) > limit
        rows = [{k: _json_safe(v) for k, v in r.items()} for r in fetched[:limit]]
        duration_ms = (time.perf_counter() - started) * 1000
        _log.info(
            "analytics_query_executed",
            tenant_id=str(tenant_id),
            row_count=len(rows),
            truncated=truncated,
            duration_ms=round(duration_ms, 2),
        )
        return AnalyticsResult(columns, rows, truncated, len(rows), duration_ms, sql_text)
