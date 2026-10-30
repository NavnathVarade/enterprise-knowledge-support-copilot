"""Usage reporting: totals per day and kind, cost per query, cost per document (Spec §106).

Unknown is never zero: cost sums cover KNOWN costs only, and every grouping reports how many
events had no price (`unpriced_events`) or no reported token count (`unreported_token_events`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.usage import UsageEvent
from packages.usage.budget import utc_day_start


@dataclass(frozen=True)
class DailyUsage:
    day: date
    kind: str
    events: int
    input_tokens: int
    output_tokens: int
    known_cost_usd: Decimal
    unpriced_events: int
    unreported_token_events: int


@dataclass(frozen=True)
class DocumentUsage:
    document_id: uuid.UUID
    events: int
    input_tokens: int
    known_cost_usd: Decimal
    unpriced_events: int


@dataclass(frozen=True)
class QueryCost:
    queries_counted: int  # requests whose every LLM call was priced
    queries_excluded_unpriced: int
    average_cost_usd: Decimal | None  # None when nothing could be counted


@dataclass(frozen=True)
class UsageSummary:
    since: datetime
    daily: list[DailyUsage]
    query_cost: QueryCost
    top_documents: list[DocumentUsage]


async def usage_summary(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    days: int = 7,
    top_documents: int = 20,
    now: datetime | None = None,
) -> UsageSummary:
    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    now = now or datetime.now(UTC)
    since = utc_day_start(now) - timedelta(days=days - 1)
    base = (UsageEvent.tenant_id == tenant_id, UsageEvent.occurred_at >= since)

    day = cast(func.timezone("UTC", UsageEvent.occurred_at), Date)
    rows = (
        await session.execute(
            select(
                day,
                UsageEvent.kind,
                func.count(),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd), 0),
                func.count().filter(UsageEvent.cost_usd.is_(None)),
                func.count().filter(UsageEvent.input_tokens.is_(None)),
            )
            .where(*base)
            .group_by(day, UsageEvent.kind)
            .order_by(day, UsageEvent.kind)
        )
    ).all()
    daily = [
        DailyUsage(r[0], r[1], r[2], int(r[3] or 0), int(r[4] or 0), Decimal(r[5] or 0), r[6], r[7])
        for r in rows
    ]

    per_request = (
        select(
            UsageEvent.request_id.label("rid"),
            func.sum(UsageEvent.cost_usd).label("cost"),
            func.count().filter(UsageEvent.cost_usd.is_(None)).label("unpriced"),
        )
        .where(
            *base,
            UsageEvent.kind == "llm",
            UsageEvent.operation == "query",
            UsageEvent.request_id.is_not(None),
        )
        .group_by(UsageEvent.request_id)
        .subquery()
    )
    q = (
        await session.execute(
            select(
                func.count().filter(per_request.c.unpriced == 0),
                func.count().filter(per_request.c.unpriced > 0),
                func.avg(per_request.c.cost).filter(per_request.c.unpriced == 0),
            )
        )
    ).one()
    query_cost = QueryCost(q[0], q[1], None if q[2] is None else Decimal(q[2]))

    doc_rows = (
        await session.execute(
            select(
                UsageEvent.document_id,
                func.count(),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.cost_usd), 0),
                func.count().filter(UsageEvent.cost_usd.is_(None)),
            )
            .where(*base, UsageEvent.document_id.is_not(None))
            .group_by(UsageEvent.document_id)
            .order_by(
                func.coalesce(func.sum(UsageEvent.cost_usd), 0).desc(),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0).desc(),
            )
            .limit(top_documents)
        )
    ).all()
    docs = [
        DocumentUsage(r[0], r[1], int(r[2] or 0), Decimal(r[3] or 0), r[4])
        for r in doc_rows
        if r[0] is not None
    ]
    return UsageSummary(since, daily, query_cost, docs)
