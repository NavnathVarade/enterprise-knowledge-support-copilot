"""Usage and budget API (Spec §106). Tenant-scoped; `admin` only."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from apps.api.app.dependencies.auth import require_role
from apps.api.app.dependencies.db import get_tenant_session
from fastapi import APIRouter, Depends, Query, Request
from packages.audit.service import record_audit
from packages.auth.identity import Identity
from packages.database.models.usage import TenantBudget
from packages.usage.budget import default_limits, get_limits, usage_today
from packages.usage.summary import usage_summary
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])
_admin = require_role("admin")


class DailyOut(BaseModel):
    day: date
    kind: str
    events: int
    input_tokens: int
    output_tokens: int
    known_cost_usd: Decimal
    unpriced_events: int
    unreported_token_events: int


class DocumentOut(BaseModel):
    document_id: UUID
    events: int
    input_tokens: int
    known_cost_usd: Decimal
    unpriced_events: int


class QueryCostOut(BaseModel):
    queries_counted: int
    queries_excluded_unpriced: int
    average_cost_usd: Decimal | None


class SummaryOut(BaseModel):
    since: datetime
    daily: list[DailyOut]
    query_cost: QueryCostOut
    top_documents: list[DocumentOut]
    note: str = (
        "Costs cover events with a configured price only; unpriced_events counts the rest. "
        "Unknown cost is not zero."
    )


class BudgetOut(BaseModel):
    daily_token_limit: int | None
    daily_cost_usd_limit: Decimal | None
    source: str  # tenant | default
    used_llm_tokens_today: int
    known_cost_usd_today: Decimal


class BudgetIn(BaseModel):
    daily_token_limit: int | None = Field(default=None, gt=0)
    daily_cost_usd_limit: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=4
    )


@router.get("/summary", response_model=SummaryOut)
async def summary(
    days: int = Query(7, ge=1, le=90),
    identity: Identity = Depends(_admin),
    session: AsyncSession = Depends(get_tenant_session),
) -> SummaryOut:
    s = await usage_summary(session, identity.tenant_id, days=days)
    return SummaryOut(
        since=s.since,
        daily=[DailyOut(**vars(d)) for d in s.daily],
        query_cost=QueryCostOut(**vars(s.query_cost)),
        top_documents=[DocumentOut(**vars(d)) for d in s.top_documents],
    )


async def _budget_view(request: Request, identity: Identity, session: AsyncSession) -> BudgetOut:
    from datetime import UTC

    defaults = default_limits(request.app.state.settings.usage)
    has_row = (await session.get(TenantBudget, identity.tenant_id)) is not None
    limits = await get_limits(session, identity.tenant_id, defaults)
    used = await usage_today(session, identity.tenant_id, datetime.now(UTC))
    return BudgetOut(
        daily_token_limit=limits.daily_token_limit,
        daily_cost_usd_limit=limits.daily_cost_usd_limit,
        source="tenant" if has_row else "default",
        used_llm_tokens_today=used.llm_tokens,
        known_cost_usd_today=used.known_cost_usd,
    )


@router.get("/budget", response_model=BudgetOut)
async def get_budget(
    request: Request,
    identity: Identity = Depends(_admin),
    session: AsyncSession = Depends(get_tenant_session),
) -> BudgetOut:
    return await _budget_view(request, identity, session)


@router.put("/budget", response_model=BudgetOut)
async def put_budget(
    request: Request,
    body: BudgetIn,
    identity: Identity = Depends(_admin),
    session: AsyncSession = Depends(get_tenant_session),
) -> BudgetOut:
    """Replaces this tenant's limits (null = unlimited for that dimension). Audited in the
    same transaction as the change."""
    row = await session.get(TenantBudget, identity.tenant_id)
    if row is None:
        row = TenantBudget(tenant_id=identity.tenant_id)
        session.add(row)
    row.daily_token_limit = body.daily_token_limit
    row.daily_cost_usd_limit = body.daily_cost_usd_limit
    row.updated_by = identity.user_id
    row.updated_at = func.clock_timestamp()
    await record_audit(
        session,
        tenant_id=identity.tenant_id,
        action="usage.budget_set",
        resource_type="budget",
        outcome="success",
        actor_user_id=identity.user_id,
        request_id=request.headers.get("x-request-id"),
        details={
            # audit details must be scalars, so "no limit" is spelled out
            "daily_token_limit": (
                "unlimited" if body.daily_token_limit is None else body.daily_token_limit
            ),
            "daily_cost_usd_limit": (
                "unlimited" if body.daily_cost_usd_limit is None else str(body.daily_cost_usd_limit)
            ),
        },
    )
    await session.commit()
    return await _budget_view(request, identity, session)
