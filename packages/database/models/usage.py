"""Usage ledger and per-tenant budgets (Spec §106).

`usage_events` is an append-only ledger written by the application: one row per LLM call
and per embedding batch. `cost_usd` is NULL when no price was configured -- unknown is
never stored as zero. `tenant_budgets` holds optional per-tenant daily limits (UTC day).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.models.base import Base, uuid_pk


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint("kind IN ('llm','embedding')", name="ck_usage_kind"),
        CheckConstraint("operation IN ('query','ingestion','other')", name="ck_usage_operation"),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND output_tokens >= 0",
            name="ck_usage_tokens_nonneg",
        ),
        Index("ix_usage_events_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_usage_events_tenant_document", "tenant_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # NULL input_tokens = the provider did not report usage (never stored as 0).
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TenantBudget(Base):
    __tablename__ = "tenant_budgets"
    __table_args__ = (
        CheckConstraint(
            "daily_token_limit IS NULL OR daily_token_limit > 0", name="ck_budget_tokens"
        ),
        CheckConstraint(
            "daily_cost_usd_limit IS NULL OR daily_cost_usd_limit > 0", name="ck_budget_cost"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    daily_token_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    daily_cost_usd_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
