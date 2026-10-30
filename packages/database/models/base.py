"""
Shared declarative base and mixins for all domain models.

Design rules enforced here (Section 9, 54-55, 116 of the architecture brief):
  - Every tenant-scoped table gets a `tenant_id` column via `TenantScopedMixin` and a
    composite index `(tenant_id, id)` — never a bare `id` index alone — since virtually
    every real query filters by tenant first.
  - All timestamps are timezone-aware UTC (`DateTime(timezone=True)`), never naive.
  - Primary keys are UUIDv4, generated application-side (not DB `gen_random_uuid()`)
    so IDs are available before insert (useful for outbox-pattern event construction
    in later phases) without a round trip.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so every constraint has a stable, predictable name --
# required for Alembic autogenerate to produce clean, reviewable migrations and for
# constraint-violation errors to be mapped to domain errors by name later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
