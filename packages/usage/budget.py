"""Per-tenant daily budgets (Spec §106), enforced before every LLM call.

Semantics
- The day is the UTC calendar day.
- A tenant WITH a `tenant_budgets` row uses that row's limits exactly (NULL = unlimited for
  that dimension). A tenant WITHOUT a row uses the environment defaults (`USAGE_DEFAULT_*`).
- `daily_token_limit` counts LLM tokens (input + output) only.
- `daily_cost_usd_limit` counts KNOWN costs across LLM and embeddings. Events with no
  configured price contribute nothing, so with no prices configured a cost limit cannot
  trigger; use the token limit in that case.
- Embedding spend is metered but never blocked: stalling ingestion mid-flight would
  create retry storms and half-indexed documents.

Known limits (documented, not hidden)
- Check-then-spend: N concurrent requests that all pass the check can overshoot the limit
  by up to N calls' worth. The overshoot is bounded by concurrency, not unbounded.
- If the check itself errors, behaviour follows `USAGE_BUDGET_FAIL_OPEN` (default allow).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.config.settings import DatabaseSettings, UsageSettings
from packages.database.models.usage import TenantBudget, UsageEvent
from packages.database.session import tenant_scoped_session
from packages.domain.errors import BudgetExceeded
from packages.observability.logging import get_logger
from packages.observability.metrics import BUDGET_REJECTIONS

_log = get_logger(component="budget_guard")


@dataclass(frozen=True)
class BudgetLimits:
    daily_token_limit: int | None
    daily_cost_usd_limit: Decimal | None

    @property
    def unlimited(self) -> bool:
        return self.daily_token_limit is None and self.daily_cost_usd_limit is None


@dataclass(frozen=True)
class DayUsage:
    llm_tokens: int
    known_cost_usd: Decimal


class BudgetGuard(Protocol):
    async def check(self, tenant_id: uuid.UUID) -> None: ...


class NoopBudgetGuard:
    async def check(self, tenant_id: uuid.UUID) -> None:
        return None


def utc_day_start(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def default_limits(settings: UsageSettings) -> BudgetLimits:
    return BudgetLimits(
        settings.default_daily_token_limit,
        None
        if settings.default_daily_cost_usd_limit is None
        else Decimal(str(settings.default_daily_cost_usd_limit)),
    )


async def get_limits(
    session: AsyncSession, tenant_id: uuid.UUID, defaults: BudgetLimits
) -> BudgetLimits:
    row = (
        await session.execute(select(TenantBudget).where(TenantBudget.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        return defaults
    return BudgetLimits(row.daily_token_limit, row.daily_cost_usd_limit)


async def usage_today(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> DayUsage:
    llm_tokens = func.coalesce(
        func.sum(func.coalesce(UsageEvent.input_tokens, 0) + UsageEvent.output_tokens).filter(
            UsageEvent.kind == "llm"
        ),
        0,
    )
    cost = func.coalesce(func.sum(UsageEvent.cost_usd), 0)
    row = (
        await session.execute(
            select(llm_tokens, cost).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.occurred_at >= utc_day_start(now),
            )
        )
    ).one()
    return DayUsage(int(row[0] or 0), Decimal(row[1] or 0))


class DbBudgetGuard:
    def __init__(
        self,
        database: DatabaseSettings,
        settings: UsageSettings,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._defaults = default_limits(settings)
        self._fail_open = settings.budget_fail_open
        self._clock = clock

    async def check(self, tenant_id: uuid.UUID) -> None:
        try:
            async with tenant_scoped_session(self._database, tenant_id=str(tenant_id)) as session:
                limits = await get_limits(session, tenant_id, self._defaults)
                if limits.unlimited:
                    return
                used = await usage_today(session, tenant_id, self._clock())
        except Exception as exc:  # noqa: BLE001
            _log.error("budget_check_failed", error=type(exc).__name__, fail_open=self._fail_open)
            if self._fail_open:
                return
            raise
        if limits.daily_token_limit is not None and used.llm_tokens >= limits.daily_token_limit:
            BUDGET_REJECTIONS.labels("token").inc()
            raise BudgetExceeded()
        if (
            limits.daily_cost_usd_limit is not None
            and used.known_cost_usd >= limits.daily_cost_usd_limit
        ):
            BUDGET_REJECTIONS.labels("cost").inc()
            raise BudgetExceeded()
