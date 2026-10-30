"""
Document domain: documents, versions, ACL entries, chunks, embeddings.

DEFENSE-IN-DEPTH TENANT ISOLATION (Section 9, 11, 13 of the architecture brief):
Every child table below references its parent via a *composite* foreign key on
`(tenant_id, parent_id)`, not just `parent_id`. This requires the parent to have a
`UNIQUE(tenant_id, id)` constraint (in addition to its primary key), and it means
Postgres itself will reject an INSERT that pairs a real `document_id` with the WRONG
`tenant_id` — e.g. a bug that lets Tenant B's chunk row reference Tenant A's document
is rejected at the database constraint level, not just caught by an application check.
This is what Section 11 means by "push security filtering into retrieval itself" taken
to its logical conclusion at the schema layer. It does not replace the application-level
tenant_id filter on every query — it backstops it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from packages.database.models.base import Base, TimestampMixin, uuid_pk
from packages.domain.document_status import DocumentStatus

_EMBEDDING_DIM = 1024  # must match EmbeddingSettings.dimensions (Section 23); see note below


class SecurityClassification(StrEnum):
    public = "PUBLIC"
    internal = "INTERNAL"
    confidential = "CONFIDENTIAL"
    restricted = "RESTRICTED"


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 hex
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=DocumentStatus.uploaded.value, index=True
    )
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    security_classification: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SecurityClassification.internal.value
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    doc_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)


class DocumentVersionStatus(StrEnum):
    draft = "draft"
    published = "published"
    superseded = "superseded"


class DocumentVersion(Base, TimestampMixin):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_document_versions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "document_id", "version", name="uq_document_versions_doc_version"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_document_versions_document",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DocumentVersionStatus.draft.value
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Temporal fields (Section 16-17): "current" is derived from these, never from
    # created_at/upload order.
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentVisibility(StrEnum):
    public_within_tenant = "public_within_tenant"
    department_only = "department_only"
    group_only = "group_only"
    user_specific = "user_specific"
    role_based = "role_based"
    restricted = "restricted"


class DocumentACL(Base, TimestampMixin):
    """
    One row per grant. A document with visibility=user_specific has one DocumentACL row
    per authorized user; department_only stores the department string directly; and so
    on. This shape (rather than a single JSON blob on Document) is what lets ACL
    filtering be pushed into a SQL WHERE/JOIN at retrieval time (Section 13) instead of
    being evaluated in application code after the fact.
    """

    __tablename__ = "document_acl"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_document_acl_document",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(30), nullable=False)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    allowed_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    allowed_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    allowed_role_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=True
    )


class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_document_chunks_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            ondelete="CASCADE",
            name="fk_document_chunks_document_version",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunking_version: Mapped[str] = mapped_column(String(50), nullable=False)
    chunk_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # Generated + stored by Postgres itself from `text` (Phase 13): it can never drift
    # out of sync with the chunk text, and application code never writes it. Backed by
    # a GIN index created in migration 0002 (autogenerate can't express the index's
    # `USING gin` form on a computed column cleanly, so it's hand-written there).
    text_search: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True), nullable=True
    )


class Embedding(Base, TimestampMixin):
    __tablename__ = "embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "chunk_id"],
            ["document_chunks.tenant_id", "document_chunks.id"],
            ondelete="CASCADE",
            name="fk_embeddings_chunk",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # NOTE on _EMBEDDING_DIM: pgvector requires a fixed dimension at column-creation
    # time. It is set from EmbeddingSettings.dimensions at import time as a best-effort
    # default (Section 23 -- "do not silently mix incompatible embedding
    # dimensions/models"); if the configured embedding model's dimension ever changes,
    # this requires an explicit migration (new column + backfill + swap), not a silent
    # schema change. See docs/adr/ADR-011-embedding-dimension-changes.md (Phase 11).
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIM), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(50), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
