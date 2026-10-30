"""
Context package types (Section 28-29).

A `ContextPackage` is the ONLY thing the generation layer (Phase 19+) receives from
retrieval. Everything in it has already passed: tenant filter, ACL, temporal check,
dedup, per-document diversity cap, version consistency and the token budget. Each
`ContextChunk` carries full citation metadata so a citation can never be generated from
anonymous text (Section 21, 33).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContextChunk:
    chunk_id: uuid.UUID
    text: str
    token_count: int
    heading_path: str | None
    page_number: int | None
    document_id: uuid.UUID
    document_title: str
    document_version: int
    security_classification: str
    source_uri: str | None
    rerank_score: float
    injection_signals: tuple[str, ...] = ()  # set for MEDIUM-risk chunks kept in context


@dataclass(frozen=True)
class ContextSelectionStats:
    """Why candidates did NOT make it into the context -- for debugging retrieval."""

    candidates_considered: int
    dropped_duplicate_count: int
    dropped_source_diversity_count: int
    dropped_token_budget_count: int
    selected_count: int
    dropped_version_inconsistent_count: int = 0
    dropped_unauthorized_count: int = 0
    dropped_not_in_effect_count: int = 0
    dropped_injection_count: int = 0


@dataclass(frozen=True)
class ContextPackage:
    chunks: list[ContextChunk]
    total_tokens: int
    truncated_by_budget: bool
    stats: ContextSelectionStats
    distinct_document_count: int = field(default=0)
