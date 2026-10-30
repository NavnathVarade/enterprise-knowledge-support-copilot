"""
Pure context-selection logic (Section 28): no database, no I/O, fully unit-testable.

Input is candidates already ordered best-first (by rerank score). Rules, applied in
this order per candidate:

  1. Version consistency: the first-seen version of a document pins that document's
     version for this context. Chunks of any other version of the same document are
     dropped, so two versions of one policy are never silently blended. The pin is held
     even if the pinning chunk is itself dropped later by dedup/cap/budget.
  2. Dedup: a chunk whose content_hash was already selected is dropped.
  3. Source diversity: at most `max_chunks_per_document` chunks per document.
  4. Token budget: a chunk that would exceed `max_tokens` is skipped (later, smaller
     chunks may still fit) and `truncated_by_budget` is set.

NOT done here: detecting semantic contradictions between DIFFERENT documents (e.g.
refund 30 days vs 14 days). That needs language understanding and is handled at
generation/citation validation (Phases 19-20); this layer only guarantees both sources
are allowed to reach the model rather than one silently winning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateInfo:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content_hash: str
    token_count: int
    rerank_score: float
    document_version: int = 1


@dataclass(frozen=True)
class SelectionResult:
    selected_ids: list[uuid.UUID]
    total_tokens: int
    truncated_by_budget: bool
    dropped_duplicate_count: int
    dropped_source_diversity_count: int
    dropped_token_budget_count: int
    dropped_version_inconsistent_count: int


def select_for_context(
    candidates: list[CandidateInfo],
    *,
    max_tokens: int,
    max_chunks_per_document: int,
) -> SelectionResult:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if max_chunks_per_document <= 0:
        raise ValueError("max_chunks_per_document must be positive")

    pinned_version: dict[uuid.UUID, int] = {}
    seen_hashes: set[str] = set()
    per_document: dict[uuid.UUID, int] = {}
    selected: list[uuid.UUID] = []
    total = 0
    truncated = False
    dup = div = budget = ver = 0

    for c in candidates:
        pin = pinned_version.setdefault(c.document_id, c.document_version)
        if c.document_version != pin:
            ver += 1
            continue
        if c.content_hash in seen_hashes:
            dup += 1
            continue
        if per_document.get(c.document_id, 0) >= max_chunks_per_document:
            div += 1
            continue
        if total + c.token_count > max_tokens:
            budget += 1
            truncated = True
            continue
        selected.append(c.chunk_id)
        seen_hashes.add(c.content_hash)
        per_document[c.document_id] = per_document.get(c.document_id, 0) + 1
        total += c.token_count

    return SelectionResult(
        selected_ids=selected,
        total_tokens=total,
        truncated_by_budget=truncated,
        dropped_duplicate_count=dup,
        dropped_source_diversity_count=div,
        dropped_token_budget_count=budget,
        dropped_version_inconsistent_count=ver,
    )
