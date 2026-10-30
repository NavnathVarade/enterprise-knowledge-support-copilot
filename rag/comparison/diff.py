"""
Deterministic chunk-level diff between two versions of one document (Spec §16, §15).
Pure: no database, no model. Chunks are matched by `content_hash`, so identical text is
"unchanged" regardless of position. Unmatched chunks are then paired in order by identical
non-null `heading_path` into "modified"; the rest are "removed" (only in A) or "added"
(only in B).

Granularity limit: the unit is the chunk, not the sentence. A one-word edit makes the
whole chunk "modified", and re-chunking (different chunk boundaries between versions) can
turn an unchanged passage into a modified/added/removed one. The LLM explanation step
sees the actual excerpts, so it can state what really changed -- but the diff itself is
only a candidate set.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    chunk_id: uuid.UUID
    content_hash: str
    heading_path: str | None
    sequence: int


@dataclass(frozen=True)
class ModifiedPair:
    before: Block
    after: Block


@dataclass(frozen=True)
class VersionDiff:
    unchanged_count: int
    added: tuple[Block, ...]
    removed: tuple[Block, ...]
    modified: tuple[ModifiedPair, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.modified)


def _unmatched(side: list[Block], other: list[Block]) -> list[Block]:
    """Blocks of `side` whose content_hash is not (or no longer) available in `other`."""
    other_counts = Counter(b.content_hash for b in other)
    seen: Counter[str] = Counter()
    out = []
    for b in side:
        seen[b.content_hash] += 1
        if seen[b.content_hash] > other_counts[b.content_hash]:
            out.append(b)
    return out


def diff_versions(before: list[Block], after: list[Block]) -> VersionDiff:
    unmatched_before = _unmatched(before, after)
    pending_after = _unmatched(after, before)
    unchanged = len(before) - len(unmatched_before)

    modified: list[ModifiedPair] = []
    removed: list[Block] = []
    for b in unmatched_before:
        partner = None
        if b.heading_path is not None:
            partner = next((a for a in pending_after if a.heading_path == b.heading_path), None)
        if partner is not None:
            pending_after.remove(partner)
            modified.append(ModifiedPair(b, partner))
        else:
            removed.append(b)
    return VersionDiff(unchanged, tuple(pending_after), tuple(removed), tuple(modified))
