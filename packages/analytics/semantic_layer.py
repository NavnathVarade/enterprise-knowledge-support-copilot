"""
Semantic layer (Spec §39-40): the ONLY description of what analytics may touch.
Anything not registered here -- table, column, function, operator -- cannot be queried.
Columns carry explicit capabilities (filterable / groupable / aggregatable) and enum
columns carry their allowed values, so a plan can be validated without ever looking at
SQL text.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from sqlalchemy import Table

from packages.database.models.analytics import (
    Customer,
    Deployment,
    Incident,
    IncidentEvent,
    SupportTicket,
    TicketEvent,
)


class Kind(enum.StrEnum):
    STR = "str"
    INT = "int"
    UUID = "uuid"
    DATETIME = "datetime"


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    kind: Kind
    description: str
    filterable: bool = True
    groupable: bool = False
    aggregatable: bool = False  # usable in sum/avg/min/max
    allowed_values: tuple[str, ...] | None = None  # closed enum, validated before querying


@dataclass(frozen=True)
class EntitySpec:
    name: str
    model: type
    description: str
    time_column: str
    columns: dict[str, ColumnSpec] = field(default_factory=dict)

    @property
    def table(self) -> Table:
        return self.model.__table__  # type: ignore[attr-defined,no-any-return]


def _cols(*specs: ColumnSpec) -> dict[str, ColumnSpec]:
    return {s.name: s for s in specs}


_SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")

ENTITIES: dict[str, EntitySpec] = {
    e.name: e
    for e in (
        EntitySpec(
            "tickets",
            SupportTicket,
            "Customer support tickets.",
            "opened_at",
            _cols(
                ColumnSpec("id", Kind.UUID, "Ticket id."),
                ColumnSpec("customer_id", Kind.UUID, "Customer id.", groupable=True),
                ColumnSpec("service_name", Kind.STR, "Affected service.", groupable=True),
                ColumnSpec(
                    "status", Kind.STR, "Ticket status.", groupable=True,
                    allowed_values=("open", "pending", "resolved", "closed"),
                ),
                ColumnSpec(
                    "priority", Kind.STR, "Ticket priority.", groupable=True,
                    allowed_values=("low", "medium", "high", "urgent"),
                ),
                ColumnSpec("channel", Kind.STR, "Intake channel.", groupable=True),
                ColumnSpec("error_code", Kind.STR, "Reported error code.", groupable=True),
                ColumnSpec("opened_at", Kind.DATETIME, "When the ticket was opened.", aggregatable=True),
                ColumnSpec("resolved_at", Kind.DATETIME, "When it was resolved.", aggregatable=True),
                ColumnSpec(
                    "first_response_seconds", Kind.INT, "Seconds to first response.", aggregatable=True
                ),
            ),
        ),
        EntitySpec(
            "ticket_events",
            TicketEvent,
            "Events on tickets (status changes, replies).",
            "occurred_at",
            _cols(
                ColumnSpec("ticket_id", Kind.UUID, "Ticket id.", groupable=True),
                ColumnSpec("event_type", Kind.STR, "Event type.", groupable=True),
                ColumnSpec("occurred_at", Kind.DATETIME, "When it happened.", aggregatable=True),
            ),
        ),
        EntitySpec(
            "incidents",
            Incident,
            "Service incidents.",
            "started_at",
            _cols(
                ColumnSpec("id", Kind.UUID, "Incident id."),
                ColumnSpec("service_name", Kind.STR, "Affected service.", groupable=True),
                ColumnSpec(
                    "severity", Kind.STR, "Severity.", groupable=True, allowed_values=_SEVERITIES
                ),
                ColumnSpec(
                    "status", Kind.STR, "Incident status.", groupable=True,
                    allowed_values=("open", "investigating", "mitigated", "resolved"),
                ),
                ColumnSpec("root_cause_category", Kind.STR, "Root-cause category.", groupable=True),
                ColumnSpec("affected_region", Kind.STR, "Affected region.", groupable=True),
                ColumnSpec(
                    "affected_customer_count", Kind.INT, "Customers affected.", aggregatable=True
                ),
                ColumnSpec("started_at", Kind.DATETIME, "Start time.", aggregatable=True),
                ColumnSpec("ended_at", Kind.DATETIME, "End time.", aggregatable=True),
            ),
        ),
        EntitySpec(
            "incident_events",
            IncidentEvent,
            "Events on incidents (timeline entries).",
            "occurred_at",
            _cols(
                ColumnSpec("incident_id", Kind.UUID, "Incident id.", groupable=True),
                ColumnSpec("event_type", Kind.STR, "Event type.", groupable=True),
                ColumnSpec("occurred_at", Kind.DATETIME, "When it happened.", aggregatable=True),
            ),
        ),
        EntitySpec(
            "deployments",
            Deployment,
            "Service deployments.",
            "deployed_at",
            _cols(
                ColumnSpec("service_name", Kind.STR, "Deployed service.", groupable=True),
                ColumnSpec("version", Kind.STR, "Deployed version.", groupable=True),
                ColumnSpec("environment", Kind.STR, "Target environment.", groupable=True),
                ColumnSpec(
                    "status", Kind.STR, "Deployment status.", groupable=True,
                    allowed_values=("succeeded", "failed", "rolled_back"),
                ),
                ColumnSpec("deployed_at", Kind.DATETIME, "Deploy time.", aggregatable=True),
            ),
        ),
        EntitySpec(
            "customers",
            Customer,
            "Customer accounts (no names or contact data are exposed).",
            "created_at",
            _cols(
                ColumnSpec("id", Kind.UUID, "Customer id."),
                ColumnSpec("plan", Kind.STR, "Plan tier.", groupable=True),
                ColumnSpec("region", Kind.STR, "Region.", groupable=True),
                ColumnSpec("created_at", Kind.DATETIME, "Signup time.", aggregatable=True),
            ),
        ),
    )
}  # fmt: skip

ALLOWED_FUNCS = ("count", "count_distinct", "sum", "avg", "min", "max")
ALLOWED_OPS = ("eq", "ne", "in", "gt", "gte", "lt", "lte")
ALLOWED_BUCKETS = ("day", "week", "month")
MAX_IN_VALUES = 50
MAX_GROUP_BY = 3
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def describe_for_prompt() -> str:
    """Compact, generated description of the semantic layer for the intent planner."""
    lines = []
    for e in ENTITIES.values():
        cols = ", ".join(
            f"{c.name}:{c.kind.value}"
            + ("[enum:" + "|".join(c.allowed_values) + "]" if c.allowed_values else "")
            for c in e.columns.values()
        )
        lines.append(f"- {e.name} ({e.description}) time_column={e.time_column}; columns: {cols}")
    return "\n".join(lines)
