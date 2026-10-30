"""
Row-level reads for investigations. Every query filters `tenant_id` explicitly and caps
rows (the reports flag truncation). Selects structured columns only -- never free text.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.analytics import (
    Customer,
    Deployment,
    Incident,
    SupportTicket,
    TicketEvent,
)
from packages.investigation.correlation import DeploymentFact, IncidentFact, TicketFact

MAX_TICKETS = 50
MAX_EVENTS = 200
MAX_INCIDENTS = 100
MAX_DEPLOYMENTS = 200


async def customer_exists(
    session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(Customer.id).where(Customer.tenant_id == tenant_id, Customer.id == customer_id)
    )
    return row.scalar_one_or_none() is not None


async def fetch_tickets(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    since: datetime,
    until: datetime,
    error_code: str | None,
    service_name: str | None,
) -> tuple[list[TicketFact], bool]:
    stmt = select(SupportTicket).where(
        SupportTicket.tenant_id == tenant_id,
        SupportTicket.customer_id == customer_id,
        SupportTicket.opened_at >= since,
        SupportTicket.opened_at < until,
    )
    if error_code is not None:
        stmt = stmt.where(SupportTicket.error_code == error_code)
    if service_name is not None:
        stmt = stmt.where(SupportTicket.service_name == service_name)
    rows = (
        (
            await session.execute(
                stmt.order_by(SupportTicket.opened_at.desc()).limit(MAX_TICKETS + 1)
            )
        )
        .scalars()
        .all()
    )
    facts = [
        TicketFact(r.id, r.service_name, r.opened_at, r.resolved_at, r.status, r.error_code)
        for r in rows[:MAX_TICKETS]
    ]
    return facts, len(rows) > MAX_TICKETS


async def fetch_ticket_events(
    session: AsyncSession, tenant_id: uuid.UUID, ticket_ids: list[uuid.UUID]
) -> list[TicketEvent]:
    if not ticket_ids:
        return []
    stmt = (
        select(TicketEvent)
        .where(TicketEvent.tenant_id == tenant_id, TicketEvent.ticket_id.in_(ticket_ids))
        .order_by(TicketEvent.occurred_at)
        .limit(MAX_EVENTS)
    )
    return list((await session.execute(stmt)).scalars().all())


async def fetch_incidents(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    since: datetime,
    until: datetime,
    services: list[str] | None = None,
    unresolved_only: bool = False,
) -> tuple[list[IncidentFact], bool]:
    """Incidents whose [started_at, ended_at or open) window intersects [since, until)."""
    stmt = select(Incident).where(
        Incident.tenant_id == tenant_id,
        Incident.started_at < until,
        (Incident.ended_at.is_(None)) | (Incident.ended_at >= since),
    )
    if services is not None:
        if not services:
            return [], False
        stmt = stmt.where(Incident.service_name.in_(services))
    if unresolved_only:
        stmt = stmt.where(Incident.status != "resolved")
    rows = (
        (await session.execute(stmt.order_by(Incident.started_at.desc()).limit(MAX_INCIDENTS + 1)))
        .scalars()
        .all()
    )
    facts = [
        IncidentFact(
            r.id,
            r.service_name,
            r.severity,
            r.status,
            r.started_at,
            r.ended_at,
            r.root_cause_category,
        )
        for r in rows[:MAX_INCIDENTS]
    ]
    return facts, len(rows) > MAX_INCIDENTS


async def fetch_deployments(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    since: datetime,
    until: datetime,
    services: list[str] | None = None,
) -> tuple[list[DeploymentFact], bool]:
    stmt = select(Deployment).where(
        Deployment.tenant_id == tenant_id,
        Deployment.deployed_at >= since,
        Deployment.deployed_at <= until,
    )
    if services is not None:
        if not services:
            return [], False
        stmt = stmt.where(Deployment.service_name.in_(services))
    rows = (
        (
            await session.execute(
                stmt.order_by(Deployment.deployed_at.desc()).limit(MAX_DEPLOYMENTS + 1)
            )
        )
        .scalars()
        .all()
    )
    facts = [
        DeploymentFact(r.id, r.service_name, r.version, r.environment, r.status, r.deployed_at)
        for r in rows[:MAX_DEPLOYMENTS]
    ]
    return facts, len(rows) > MAX_DEPLOYMENTS
