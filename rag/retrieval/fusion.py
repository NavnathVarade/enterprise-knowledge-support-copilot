"""
Reciprocal Rank Fusion (Section 25, ADR-006).

score(id) = sum over every ranked list containing `id` of 1 / (k + rank), where rank is
the item's 1-indexed position in that list. An item missing from a list contributes 0
from that list -- it is not penalized beyond simply not getting that list's boost.

RRF was chosen over weighted score-fusion (Section 25's other named option) because it
needs no cross-retriever score calibration: pgvector's cosine distance and Postgres
full-text's `ts_rank_cd` live on entirely different, incomparable scales, and RRF sidesteps
that by only ever looking at RANK, never the underlying score value. `k` (default 60,
the value used in the original RRF paper and widely adopted since) controls how quickly
the boost decays with rank -- larger k flattens the difference between a rank-1 and a
rank-5 result; smaller k sharpens it.

Deliberately pure: no I/O, no database. `hybrid.py` is the thin layer that turns real
retriever output into the plain ID lists this function consumes.
"""

from __future__ import annotations

import uuid


def reciprocal_rank_fusion(
    ranked_lists: list[list[uuid.UUID]], *, k: int = 60
) -> list[tuple[uuid.UUID, float]]:
    """
    Each inner list is one retriever's results, best match first. Returns
    (id, fused_score) pairs sorted by fused_score descending, ties broken by the id's
    string representation for a fully deterministic order (never by insertion order,
    which would make output depend on incidental list construction).
    """
    scores: dict[uuid.UUID, float] = {}
    for ranked_list in ranked_lists:
        for rank, item_id in enumerate(ranked_list, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda pair: (-pair[1], str(pair[0])))
