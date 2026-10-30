"""
Pure temporal-correlation logic for support/incident investigation (Spec §42-43).
No database, no clock (callers pass `now`), fully unit-testable.

IMPORTANT HONESTY RULE: everything here is TEMPORAL CORRELATION, not causation. A
deployment shortly before an incident is a lead worth checking, never a proven root
cause, and results are labeled that way. Strength levels come from stated rules, not
from a statistical model, and are not probabilities.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TicketFact:
    id: uuid.UUID
    service_name: str
    opened_at: datetime
    resolved_at: datetime | None
    status: str
    error_code: str | None


@dataclass(frozen=True)
class IncidentFact:
    id: uuid.UUID
    service_name: str
    severity: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    root_cause_category: str | None


@dataclass(frozen=True)
class DeploymentFact:
    id: uuid.UUID
    service_name: str
    version: str
    environment: str
    status: str
    deployed_at: datetime


@dataclass(frozen=True)
class Candidate:
    kind: str  # "incident" | "deployment"
    ref_id: uuid.UUID
    service_name: str
    strength: str  # "strong" | "moderate"
    reason: str  # the rule that fired, in words
    at: datetime
    related_ticket_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    related_incident_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TimelineEvent:
    at: datetime
    kind: str  # ticket_opened | ticket_resolved | ticket_event | incident_started | incident_ended | deployment
    ref_id: uuid.UUID
    service_name: str | None
    detail: str  # structured labels only (status/type/version) -- never free text


def incident_overlaps_ticket(
    incident: IncidentFact, ticket: TicketFact, *, grace: timedelta, now: datetime
) -> bool:
    if incident.service_name != ticket.service_name:
        return False
    incident_end = incident.ended_at or now
    ticket_end = ticket.resolved_at or ticket.opened_at
    return incident.started_at <= ticket_end + grace and incident_end + grace >= ticket.opened_at


def correlated_incidents(
    tickets: list[TicketFact],
    incidents: list[IncidentFact],
    *,
    grace: timedelta,
    now: datetime,
) -> list[tuple[IncidentFact, tuple[uuid.UUID, ...]]]:
    out = []
    for inc in incidents:
        covered = tuple(
            t.id for t in tickets if incident_overlaps_ticket(inc, t, grace=grace, now=now)
        )
        if covered:
            out.append((inc, covered))
    return out


def deployments_before(
    at: datetime, service_name: str, deployments: list[DeploymentFact], *, lookback: timedelta
) -> list[DeploymentFact]:
    """Deployments to `service_name` within `lookback` BEFORE `at`, closest first."""
    hits = [
        d
        for d in deployments
        if d.service_name == service_name and at - lookback <= d.deployed_at <= at
    ]
    return sorted(hits, key=lambda d: at - d.deployed_at)


_BAD_DEPLOY = frozenset({"failed", "rolled_back"})
_SEVERE = frozenset({"SEV1", "SEV2"})
_STRONG_DEPLOY_WINDOW = timedelta(hours=6)


def rank_candidates(
    tickets: list[TicketFact],
    incidents: list[IncidentFact],
    deployments: list[DeploymentFact],
    *,
    grace: timedelta = timedelta(hours=1),
    lookback: timedelta = timedelta(hours=24),
    now: datetime,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    linked = correlated_incidents(tickets, incidents, grace=grace, now=now)

    for inc, ticket_ids in linked:
        strong = len(ticket_ids) >= 2 or inc.severity in _SEVERE
        why = (
            f"{inc.severity} incident on {inc.service_name} overlapped "
            f"{len(ticket_ids)} of the customer's ticket(s)"
        )
        candidates.append(
            Candidate(
                "incident",
                inc.id,
                inc.service_name,
                "strong" if strong else "moderate",
                why,
                inc.started_at,
                related_ticket_ids=ticket_ids,
            )
        )

    seen: set[uuid.UUID] = set()
    anchors: list[tuple[datetime, str, tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]] = []
    for inc, ticket_ids in linked:
        anchors.append((inc.started_at, inc.service_name, ticket_ids, (inc.id,)))
    for t in tickets:
        anchors.append((t.opened_at, t.service_name, (t.id,), ()))

    for at, service, ticket_ids, incident_ids in anchors:
        for d in deployments_before(at, service, deployments, lookback=lookback):
            if d.id in seen:
                continue
            seen.add(d.id)
            gap = at - d.deployed_at
            near = gap <= _STRONG_DEPLOY_WINDOW
            bad = d.status in _BAD_DEPLOY
            strong = near and (bad or bool(incident_ids))
            hours = round(gap.total_seconds() / 3600, 1)
            target = "incident start" if incident_ids else "ticket opening"
            candidates.append(
                Candidate(
                    "deployment",
                    d.id,
                    service,
                    "strong" if strong else "moderate",
                    f"{d.status} deployment of {service} v{d.version} ({d.environment}) "
                    f"{hours}h before {target}",
                    d.deployed_at,
                    related_ticket_ids=ticket_ids,
                    related_incident_ids=incident_ids,
                )
            )

    order = {"strong": 0, "moderate": 1}
    return sorted(candidates, key=lambda c: (order[c.strength], -c.at.timestamp()))
