"""Document lifecycle states. Lives in the domain layer so the lifecycle rules and the ORM models
both depend on it, not on each other."""

from __future__ import annotations

from enum import StrEnum


class DocumentStatus(StrEnum):
    uploaded = "uploaded"
    validating = "validating"
    processing = "processing"
    parsed = "parsed"
    chunked = "chunked"
    embedding = "embedding"
    indexing = "indexing"
    active = "active"
    processing_failed = "processing_failed"
    embedding_failed = "embedding_failed"
    indexing_failed = "indexing_failed"
    quarantined = "quarantined"
    deleted = "deleted"
    archived = "archived"
