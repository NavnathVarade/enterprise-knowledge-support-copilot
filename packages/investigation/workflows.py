"""
Support and incident investigation workflows (Spec §42-43).

Gather evidence across tickets -> ticket events -> incidents -> deployments -> knowledge
articles -> error documentation, then rank TEMPORAL-CORRELATION leads. Deterministic: no
LLM is involved, so every claim in a report traces to a row id. (A later phase may ask a
model to *narrate* a report, with citations, but never to invent evidence.)

Authorization: investigations read row-level operational data, so the caller must hold a
role from `allowed_roles` (default support_agent/admin) AND the tenant comes only from the
authenticated Identity. Knowledge lookups go through an injected callable; the provided
`make_authorized_knowledge_search` uses ACL-enforced retrieval, so a document the user
cannot read can never appear as a knowledge reference.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from packages.auth.identity import Identity
from packages.database.models.analytics import TicketEvent
from packages.domain.errors import InvalidQuery, PermissionDenied, ResourceNotFound
from packages.investigation import repository as repo
from packages.investigation.correlation import (
    Candidate,
    DeploymentFact,
    IncidentFact,
    TicketFact,
    TimelineEvent,
    deployments_before,
    rank_candidates,
)

DEFAULT_ALLOWED_ROLES = frozenset({"support_agent", "admin"})
MAX_WINDOW = timedelta(days=366)


@dataclass(frozen=True)
class KnowledgeRef:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    score: float
    kind: str  # "knowledge_article" | "error_documentation"


KnowledgeSearch = Callable[[str, str], Awaitable[list[KnowledgeRef]]]  # (query, kind)


@dataclass(frozen=True)
class InvestigationReport:
    customer_id: uuid.UUID
    window_start: datetime
    window_end: datetime
    tickets: list[TicketFact]
    incidents: list[IncidentFact]
    deployments: list[DeploymentFact]
    candidates: list[Candidate]
    timeline: list[TimelineEvent]
    knowledge: list[KnowledgeRef]
    truncated: dict[str, bool] = field(default_factory=dict)
    disclaimer: str = (
        "Candidates are temporal correlations found in the data, not confirmed root causes."
    )


@dataclass(frozen=True)
class RecurringPattern:
    service_name: str
    root_cause_category: str
    incident_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class IncidentReport:
    window_start: datetime
    window_end: datetime
    unresolved: list[IncidentFact]
    incidents: list[IncidentFact]
    recurring: list[RecurringPattern]
    preceding_deployments: dict[uuid.UUID, list[DeploymentFact]]  # incident id -> deploys
    truncated: dict[str, bool] = field(default_factory=dict)


def _authorize(identity: Identity, allowed_roles: frozenset[str]) -> None:
    if not identity.roles.intersection(allowed_roles):
        raise PermissionDenied()


def _window(
    since: datetime | None, until: datetime | None, now: datetime, default: timedelta
) -> tuple[datetime, datetime]:
    end = until or now
    start = since or end - default
    if start.tzinfo is None or end.tzinfo is None:
        raise InvalidQuery("Time bounds must be timezone-aware.")
    if start >= end:
        raise InvalidQuery("Window start must be before end.")
    if end - start > MAX_WINDOW:
        raise InvalidQuery("Window is too large.")
    return start, end


async def investigate_customer_issue(
    session: AsyncSession,
    *,
    identity: Identity,
    customer_id: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
    error_code: str | None = None,
    service_name: str | None = None,
    knowledge_search: KnowledgeSearch | None = None,
    now: datetime | None = None,
    allowed_roles: frozenset[str] = DEFAULT_ALLOWED_ROLES,
    lookback: timedelta = timedelta(hours=24),
    grace: timedelta = timedelta(hours=1),
) -> InvestigationReport:
    _authorize(identity, allowed_roles)
    now = now or datetime.now(UTC)
    start, end = _window(since, until, now, timedelta(days=30))
    tenant = identity.tenant_id

    if not await repo.customer_exists(session, tenant, customer_id):
        raise ResourceNotFound()  # also the answer for another tenant's customer

    tickets, t_trunc = await repo.fetch_tickets(
        session,
        tenant,
        customer_id,
        since=start,
        until=end,
        error_code=error_code,
        service_name=service_name,
    )
    events = await repo.fetch_ticket_events(session, tenant, [t.id for t in tickets])
    services = sorted({t.service_name for t in tickets})

    incidents: list[IncidentFact] = []
    deployments: list[DeploymentFact] = []
    i_trunc = d_trunc = False
    if tickets:
        earliest = min(t.opened_at for t in tickets)
        incidents, i_trunc = await repo.fetch_incidents(
            session, tenant, since=earliest - grace, until=end + grace, services=services
        )
        deployments, d_trunc = await repo.fetch_deployments(
            session,
            tenant,
            since=earliest - lookback - timedelta(hours=24),
            until=end,
            services=services,
        )

    candidates = rank_candidates(
        tickets, incidents, deployments, grace=grace, lookback=lookback, now=now
    )

    knowledge: list[KnowledgeRef] = []
    if knowledge_search is not None:
        if error_code or any(t.error_code for t in tickets):
            code = error_code or next(t.error_code for t in tickets if t.error_code)
            knowledge += await knowledge_search(code, "error_documentation")
        for svc in services[:3]:
            knowledge += await knowledge_search(f"{svc} troubleshooting", "knowledge_article")

    timeline = _timeline(tickets, events, incidents, deployments, candidates)
    return InvestigationReport(
        customer_id=customer_id,
        window_start=start,
        window_end=end,
        tickets=tickets,
        incidents=[i for i in incidents if any(c.ref_id == i.id for c in candidates)],
        deployments=[d for d in deployments if any(c.ref_id == d.id for c in candidates)],
        candidates=candidates,
        timeline=timeline,
        knowledge=knowledge,
        truncated={"tickets": t_trunc, "incidents": i_trunc, "deployments": d_trunc},
    )


def _timeline(
    tickets: list[TicketFact],
    events: list[TicketEvent],
    incidents: list[IncidentFact],
    deployments: list[DeploymentFact],
    candidates: list[Candidate],
) -> list[TimelineEvent]:
    relevant = {c.ref_id for c in candidates}
    out: list[TimelineEvent] = []
    for t in tickets:
        out.append(
            TimelineEvent(t.opened_at, "ticket_opened", t.id, t.service_name, f"status={t.status}")
        )
        if t.resolved_at:
            out.append(
                TimelineEvent(t.resolved_at, "ticket_resolved", t.id, t.service_name, "resolved")
            )
    for e in events:
        out.append(TimelineEvent(e.occurred_at, "ticket_event", e.id, None, f"type={e.event_type}"))
    for i in incidents:
        if i.id in relevant:
            out.append(
                TimelineEvent(
                    i.started_at, "incident_started", i.id, i.service_name, f"severity={i.severity}"
                )
            )
            if i.ended_at:
                out.append(
                    TimelineEvent(
                        i.ended_at, "incident_ended", i.id, i.service_name, f"status={i.status}"
                    )
                )
    for d in deployments:
        if d.id in relevant:
            out.append(
                TimelineEvent(
                    d.deployed_at,
                    "deployment",
                    d.id,
                    d.service_name,
                    f"v{d.version} {d.status} ({d.environment})",
                )
            )
    return sorted(out, key=lambda e: e.at)


async def analyze_incidents(
    session: AsyncSession,
    *,
    identity: Identity,
    service_name: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
    allowed_roles: frozenset[str] = DEFAULT_ALLOWED_ROLES,
    lookback: timedelta = timedelta(hours=24),
    recurrence_threshold: int = 2,
) -> IncidentReport:
    """Unresolved incidents, recurring patterns, and "what changed before" per incident."""
    _authorize(identity, allowed_roles)
    now = now or datetime.now(UTC)
    start, end = _window(since, until, now, timedelta(days=90))
    tenant = identity.tenant_id
    services = [service_name] if service_name else None

    incidents, i_trunc = await repo.fetch_incidents(
        session, tenant, since=start, until=end, services=services
    )
    unresolved = [i for i in incidents if i.status != "resolved"]

    groups: dict[tuple[str, str], list[uuid.UUID]] = defaultdict(list)
    for i in incidents:
        if i.root_cause_category:
            groups[(i.service_name, i.root_cause_category)].append(i.id)
    recurring = [
        RecurringPattern(svc, cause, tuple(ids))
        for (svc, cause), ids in sorted(groups.items())
        if len(ids) >= recurrence_threshold
    ]

    svc_list = sorted({i.service_name for i in incidents})
    deployments: list[DeploymentFact] = []
    d_trunc = False
    if incidents:
        deployments, d_trunc = await repo.fetch_deployments(
            session,
            tenant,
            since=min(i.started_at for i in incidents) - lookback,
            until=end,
            services=svc_list,
        )
    preceding = {
        i.id: deployments_before(i.started_at, i.service_name, deployments, lookback=lookback)
        for i in incidents
    }
    return IncidentReport(
        window_start=start,
        window_end=end,
        unresolved=unresolved,
        incidents=incidents,
        recurring=recurring,
        preceding_deployments={k: v for k, v in preceding.items() if v},
        truncated={"incidents": i_trunc, "deployments": d_trunc},
    )
