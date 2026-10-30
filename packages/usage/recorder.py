"""Usage ledger writer. Best effort by design: metering must never break an answer, so a
write failure is logged and counted (`ekc_usage_record_failures_total`) and the ledger
under-counts by exactly that many events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from packages.config.settings import DatabaseSettings
from packages.database.models.usage import UsageEvent
from packages.database.session import tenant_scoped_session
from packages.observability.logging import get_logger
from packages.observability.metrics import USAGE_RECORD_FAILURES
from packages.usage.context import current_context

_log = get_logger(component="usage_recorder")


@dataclass(frozen=True)
class UsageRecord:
    tenant_id: uuid.UUID
    kind: str  # llm | embedding
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int
    cost_usd: float | None  # None = unknown (no price configured), never 0


class UsageRecorder(Protocol):
    async def record(self, record: UsageRecord) -> None: ...


class NoopUsageRecorder:
    async def record(self, record: UsageRecord) -> None:
        return None


class DbUsageRecorder:
    def __init__(self, database: DatabaseSettings) -> None:
        self._database = database

    async def record(self, record: UsageRecord) -> None:
        ctx = current_context()
        try:
            async with tenant_scoped_session(
                self._database, tenant_id=str(record.tenant_id)
            ) as session:
                session.add(
                    UsageEvent(
                        tenant_id=record.tenant_id,
                        kind=record.kind,
                        operation=ctx.operation,
                        provider=record.provider[:64],
                        model=record.model[:128],
                        input_tokens=record.input_tokens,
                        output_tokens=record.output_tokens,
                        cost_usd=None
                        if record.cost_usd is None
                        else Decimal(f"{record.cost_usd:.8f}"),
                        user_id=ctx.user_id,
                        document_id=ctx.document_id,
                        request_id=ctx.request_id,
                    )
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 -- metering must not break the caller
            USAGE_RECORD_FAILURES.labels(record.kind).inc()
            _log.error("usage_record_failed", kind=record.kind, error=type(exc).__name__)
