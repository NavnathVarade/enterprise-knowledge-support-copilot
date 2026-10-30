"""
Operational analytics tables (Section 39): services-adjacent business data queried ONLY
through the semantic layer in packages/analytics (never by LLM-written SQL).

Deliberate choices:
  - Every table is tenant-scoped (`tenant_id` NOT NULL, indexed) and has Postgres
    row-level security (migration 0004) as a second wall behind the query builder's
    explicit tenant filter.
  - No free-text/PII columns that analytics needs (no customer names/emails, no ticket
    bodies): the semantic layer can only expose columns that exist and are safe to
    aggregate. Free text belongs in the document/RAG path where ACLs apply.
  - `service_name` is a plain string instead of a FK to a services table: v1 analytics
    avoids joins entirely (fewer ways to leak across tables). A services dimension table
    is a documented later step.
Not built: `orders` and `products` from the spec's example schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.models.base import Base, TimestampMixin, uuid_pk


def _tenant_col() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    plan: Mapped[str] = mapped_column(String(50), nullable=False)
    region: Mapped[str] = mapped_column(String(50), nullable=False)


class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_support_tickets_tenant_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_response_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TicketEvent(Base, TimestampMixin):
    __tablename__ = "ticket_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["support_tickets.tenant_id", "support_tickets.id"],
            ondelete="CASCADE",
            name="fk_ticket_events_ticket",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Incident(Base, TimestampMixin):
    __tablename__ = "incidents"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_incidents_tenant_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    root_cause_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    affected_region: Mapped[str | None] = mapped_column(String(50), nullable=True)
    affected_customer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IncidentEvent(Base, TimestampMixin):
    __tablename__ = "incident_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "incident_id"],
            ["incidents.tenant_id", "incidents.id"],
            ondelete="CASCADE",
            name="fk_incident_events_incident",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    incident_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Deployment(Base, TimestampMixin):
    __tablename__ = "deployments"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_deployments_tenant_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = _tenant_col()
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    environment: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
