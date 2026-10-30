"""analytics tables + row-level security

Revision ID: 0004_analytics_tables
Revises: 0003_prompt_versions
Create Date: 2026-10-12

Creates customers, support_tickets, ticket_events, incidents, incident_events,
deployments. Each table gets ROW LEVEL SECURITY with a SELECT policy that matches
`tenant_id` against the `app.current_tenant_id` setting (set per transaction by the
analytics executor). The setting being unset/empty yields NULL -> no rows (fail closed).

RLS here is ENABLE (not FORCE): the table owner -- the application's migration/ingest
role -- bypasses it by design; it binds the non-owner read-only analytics role, which
is the only role the semantic layer's executor connects as in production. See
infrastructure/sql/analytics_readonly_role.sql.

Pure "expand" change (new tables), safe to deploy before the code.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_analytics_tables"
down_revision: str | None = "0003_prompt_versions"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None

_TABLES = (
    "customers",
    "support_tickets",
    "ticket_events",
    "incidents",
    "incident_events",
    "deployments",
)


def _pk_and_tenant() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def _ts() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _dt(name: str, nullable: bool = False) -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "customers",
        *_pk_and_tenant(),
        sa.Column("plan", sa.String(50), nullable=False),
        sa.Column("region", sa.String(50), nullable=False),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
    )
    op.create_table(
        "support_tickets",
        *_pk_and_tenant(),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(50), nullable=True),
        _dt("opened_at"),
        _dt("resolved_at", True),
        sa.Column("first_response_seconds", sa.Integer, nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_support_tickets_tenant_id_id"),
    )
    op.create_table(
        "ticket_events",
        *_pk_and_tenant(),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        _dt("occurred_at"),
        *_ts(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["support_tickets.tenant_id", "support_tickets.id"],
            ondelete="CASCADE",
            name="fk_ticket_events_ticket",
        ),
    )
    op.create_table(
        "incidents",
        *_pk_and_tenant(),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("root_cause_category", sa.String(50), nullable=True),
        sa.Column("affected_region", sa.String(50), nullable=True),
        sa.Column("affected_customer_count", sa.Integer, nullable=False),
        _dt("started_at"),
        _dt("ended_at", True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_incidents_tenant_id_id"),
    )
    op.create_table(
        "incident_events",
        *_pk_and_tenant(),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        _dt("occurred_at"),
        *_ts(),
        sa.ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            ondelete="CASCADE",
            name="fk_incident_events_incident",
        ),
    )
    op.create_table(
        "deployments",
        *_pk_and_tenant(),
        sa.Column("service_name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("environment", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        _dt("deployed_at"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_deployments_tenant_id_id"),
    )

    for table in _TABLES:
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation_select ON {table} FOR SELECT "
            f"USING (tenant_id = nullif(current_setting('app.current_tenant_id', true), '')::uuid)"
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
