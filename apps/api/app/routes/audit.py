"""Audit log read API: tenant-scoped, role-gated (`admin` or `security_auditor`)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from apps.api.app.dependencies.auth import require_role
from apps.api.app.dependencies.db import get_tenant_session
from fastapi import APIRouter, Depends, Query
from packages.audit.service import list_audit_events, verify_chain
from packages.auth.identity import Identity
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
_auditor = require_role("admin", "security_auditor")


class AuditEventOut(BaseModel):
    seq: int
    occurred_at: datetime
    actor_user_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    request_id: str | None
    details: dict[str, object]


class ChainOut(BaseModel):
    ok: bool
    checked: int
    first_bad_seq: int | None
    reason: str | None


@router.get("", response_model=list[AuditEventOut])
async def list_events(
    limit: int = Query(50, ge=1, le=200),
    before_seq: int | None = Query(None, ge=1),
    identity: Identity = Depends(_auditor),
    session: AsyncSession = Depends(get_tenant_session),
) -> list[AuditEventOut]:
    rows = await list_audit_events(session, identity.tenant_id, limit=limit, before_seq=before_seq)
    return [
        AuditEventOut(
            seq=r.seq, occurred_at=r.occurred_at, actor_user_id=r.actor_user_id,
            action=r.action, resource_type=r.resource_type, resource_id=r.resource_id,
            outcome=r.outcome, request_id=r.request_id, details=r.details,
        )
        for r in rows
    ]  # fmt: skip


@router.get("/verify", response_model=ChainOut)
async def verify(
    identity: Identity = Depends(_auditor),
    session: AsyncSession = Depends(get_tenant_session),
) -> ChainOut:
    v = await verify_chain(session, identity.tenant_id)
    return ChainOut(ok=v.ok, checked=v.checked, first_bad_seq=v.first_bad_seq, reason=v.reason)
