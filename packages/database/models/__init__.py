"""
Import every model module so `Base.metadata` is complete when Alembic (or anything
else) inspects it. Individual model classes are imported here purely for their
side effect of registering with `Base.metadata` — re-exporting them lets callers do
`from packages.database.models import Document` without knowing the submodule.
"""

from __future__ import annotations

from packages.database.models.analytics import (
    Customer,
    Deployment,
    Incident,
    IncidentEvent,
    SupportTicket,
    TicketEvent,
)
from packages.database.models.audit import AuditEvent
from packages.database.models.base import Base
from packages.database.models.documents import (
    Document,
    DocumentACL,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionStatus,
    DocumentVisibility,
    Embedding,
    SecurityClassification,
)
from packages.database.models.identity import (
    Group,
    Role,
    Tenant,
    TenantStatus,
    User,
    UserGroup,
    UserRole,
    UserStatus,
)
from packages.database.models.outbox import OutboxEvent
from packages.database.models.prompts import PromptVersion
from packages.database.models.usage import TenantBudget, UsageEvent
from packages.domain.document_status import DocumentStatus

__all__ = [
    "AuditEvent",
    "OutboxEvent",
    "Base",
    "Tenant",
    "TenantStatus",
    "User",
    "UserStatus",
    "Role",
    "UserRole",
    "Group",
    "UserGroup",
    "Document",
    "DocumentStatus",
    "SecurityClassification",
    "DocumentVersion",
    "DocumentVersionStatus",
    "DocumentACL",
    "DocumentVisibility",
    "DocumentChunk",
    "Embedding",
    "PromptVersion",
    "Customer",
    "SupportTicket",
    "TicketEvent",
    "Incident",
    "IncidentEvent",
    "Deployment",
    "TenantBudget",
    "UsageEvent",
]
