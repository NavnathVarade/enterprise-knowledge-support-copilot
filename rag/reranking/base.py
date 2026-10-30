"""
Reranker interface (Section 27, 105).

Every reranking call goes through this Protocol, never a vendor SDK or model library
directly -- the same discipline as `rag/embeddings/base.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: uuid.UUID
    text: str


@dataclass(frozen=True)
class RerankResult:
    chunk_id: uuid.UUID
    score: float


class RerankerProvider(Protocol):
    async def rerank(self, query: str, candidates: list[RerankCandidate]) -> list[RerankResult]:
        """Returns results sorted by score descending. May return fewer items than
        given (a reranker is allowed to drop clearly-irrelevant candidates), but must
        never return an item not present in `candidates`."""
        ...
