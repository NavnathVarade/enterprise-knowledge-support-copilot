"""
Audit trail service (Spec §59, §62).

What is recorded: who (user id), what (action), on what (resource type/id), outcome,
request id, and a SMALL allow-listed `details` map. What is never recorded: query text,
prompts, document content, emails, filenames, passwords, tokens. `details` keys outside the
allow-list raise at write time (a programming error), so content cannot leak in by accident.

Tamper evidence: each row stores `prev_hash` (the previous row's `row_hash` for the same
tenant, or 64 zeros) and `row_hash = sha256(prev_hash || canonical row)`. Writers serialize
per tenant with a transaction-scoped advisory lock so `seq` is gapless and the chain linear.
`verify_chain` recomputes it. This detects edits/deletions by anyone who cannot also
recompute the whole chain; a database superuser who can drop the append-only triggers AND
rewrite every later row defeats it -- anchoring chain heads in external WORM storage is the
documented next step, NOT built.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.audit import AuditEvent

GENESIS_HASH = "0" * 64
OUTCOMES = frozenset({"success", "denied", "failure"})
ALLOWED_DETAIL_KEYS = frozenset(
    {
        "route", "status", "query_hash", "mime_type", "size_bytes", "reason", "error_code",
        "document_version", "version_a", "version_b", "required_roles",
        "daily_token_limit", "daily_cost_usd_limit",
    }
)  # fmt: skip
_MAX_DETAIL_STR = 200


class AuditDetailRejected(ValueError):
    pass


def _clean_details(details: dict[str, Any]) -> dict[str, Any]:
    bad = set(details) - ALLOWED_DETAIL_KEYS
    if bad:
        raise AuditDetailRejected(f"audit detail keys not allowed: {sorted(bad)}")
    for key, value in details.items():
        if isinstance(value, str) and len(value) > _MAX_DETAIL_STR:
            raise AuditDetailRejected(f"audit detail '{key}' too long")
        if not isinstance(value, str | int | float | bool | list):
            raise AuditDetailRejected(f"audit detail '{key}' must be a scalar or list")
    return details


def compute_row_hash(
    *,
    prev_hash: str,
    tenant_id: uuid.UUID,
    seq: int,
    occurred_at: datetime,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    outcome: str,
    request_id: str | None,
    details: dict[str, Any],
) -> str:
    canonical = json.dumps(
        [
            str(tenant_id), seq, occurred_at.isoformat(),
            str(actor_user_id) if actor_user_id else None,
            action, resource_type, resource_id, outcome, request_id, details,
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )  # fmt: skip
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


async def record_audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: str,
    resource_type: str,
    outcome: str,
    actor_user_id: uuid.UUID | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Adds one event to the CALLER's transaction (commits with it). Serialized per tenant."""
    if outcome not in OUTCOMES:
        raise ValueError(f"invalid audit outcome {outcome!r}")
    clean = _clean_details(dict(details or {}))
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"audit:{tenant_id}"}
    )
    last = (
        await session.execute(
            select(AuditEvent.seq, AuditEvent.row_hash)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    seq = 1 if last is None else last[0] + 1
    prev_hash = GENESIS_HASH if last is None else last[1]
    now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    row = AuditEvent(
        tenant_id=tenant_id,
        seq=seq,
        occurred_at=now,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        request_id=request_id,
        details=clean,
        prev_hash=prev_hash,
        row_hash=compute_row_hash(
            prev_hash=prev_hash, tenant_id=tenant_id, seq=seq, occurred_at=now,
            actor_user_id=actor_user_id, action=action, resource_type=resource_type,
            resource_id=resource_id, outcome=outcome, request_id=request_id, details=clean,
        ),
    )  # fmt: skip
    session.add(row)
    await session.flush()
    return row


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    checked: int
    first_bad_seq: int | None
    reason: str | None


async def verify_chain(session: AsyncSession, tenant_id: uuid.UUID) -> ChainVerification:
    rows = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq)
        )
    ).scalars()
    prev, expected_seq, checked = GENESIS_HASH, 1, 0
    for r in rows:
        if r.seq != expected_seq:
            return ChainVerification(False, checked, expected_seq, "gap in sequence")
        if r.prev_hash != prev:
            return ChainVerification(False, checked, r.seq, "prev_hash mismatch")
        recomputed = compute_row_hash(
            prev_hash=r.prev_hash, tenant_id=r.tenant_id, seq=r.seq, occurred_at=r.occurred_at,
            actor_user_id=r.actor_user_id, action=r.action, resource_type=r.resource_type,
            resource_id=r.resource_id, outcome=r.outcome, request_id=r.request_id,
            details=r.details,
        )  # fmt: skip
        if recomputed != r.row_hash:
            return ChainVerification(False, checked, r.seq, "row content does not match its hash")
        prev, expected_seq, checked = r.row_hash, expected_seq + 1, checked + 1
    return ChainVerification(True, checked, None, None)


async def list_audit_events(
    session: AsyncSession, tenant_id: uuid.UUID, *, limit: int = 50, before_seq: int | None = None
) -> list[AuditEvent]:
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if before_seq is not None:
        stmt = stmt.where(AuditEvent.seq < before_seq)
    rows = await session.execute(stmt.order_by(AuditEvent.seq.desc()).limit(min(limit, 200)))
    return list(rows.scalars().all())
