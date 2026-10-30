"""usage ledger and tenant budgets

Revision ID: 0007_usage_ledger
Revises: 0006_audit_events
Create Date: 2026-10-18

Pure "expand" change (two new tables). RLS SELECT policies match the analytics tables:
reads through a tenant-scoped session only see that tenant's rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_usage_ledger"
down_revision: str | None = "0006_audit_events"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None

_POLICY = "tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.CheckConstraint("kind IN ('llm','embedding')", name="ck_usage_kind"),
        sa.CheckConstraint("operation IN ('query','ingestion','other')", name="ck_usage_operation"),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND output_tokens >= 0",
            name="ck_usage_tokens_nonneg",
        ),
    )
    op.create_index("ix_usage_events_tenant_time", "usage_events", ["tenant_id", "occurred_at"])
    op.create_index("ix_usage_events_tenant_document", "usage_events", ["tenant_id", "document_id"])

    op.create_table(
        "tenant_budgets",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("daily_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("daily_cost_usd_limit", sa.Numeric(14, 4), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "daily_token_limit IS NULL OR daily_token_limit > 0", name="ck_budget_tokens"
        ),
        sa.CheckConstraint(
            "daily_cost_usd_limit IS NULL OR daily_cost_usd_limit > 0", name="ck_budget_cost"
        ),
    )
    for table in ("usage_events", "tenant_budgets"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation_select ON {table} FOR SELECT USING ({_POLICY})")


def downgrade() -> None:
    op.drop_table("tenant_budgets")
    op.drop_table("usage_events")
