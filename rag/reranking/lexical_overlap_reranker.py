"""
Local lexical-overlap reranker.

HONEST LABELING: this is NOT a neural cross-encoder. A real cross-encoder reranker
requires downloading trained model weights, which -- like Kafka's broker in Phase 8 and
a live embedding provider in Phase 11 -- has no reachable source from this environment
(model hosts like huggingface.co are outside the network allowlist). This is a real,
deterministic scoring algorithm instead: term-coverage-weighted lexical overlap between
the query and each candidate's text.

Scoring, in order of priority (matching how a person would judge relevance):
  1. Coverage: what fraction of the query's distinct terms appear in the candidate at
     all. A candidate matching 4 of 5 query terms should usually beat one matching 1 of
     5, regardless of how many times that one term repeats.
  2. Density (tie-breaker): among candidates with equal coverage, how frequently the
     matched terms appear relative to the candidate's length.

This exists specifically to REORDER a small candidate set (Phase 14's fused top-K) more
carefully than the coarse retrieval-stage scores could -- it is not meant to replace
lexical search's index-backed retrieval role from Phase 13.
"""

from __future__ import annotations

import re
from collections import Counter

from rag.reranking.base import RerankCandidate, RerankResult

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


class LexicalOverlapReranker:
    async def rerank(self, query: str, candidates: list[RerankCandidate]) -> list[RerankResult]:
        query_terms = set(_tokenize(query))
        if not query_terms:
            return [RerankResult(chunk_id=c.chunk_id, score=0.0) for c in candidates]

        results: list[RerankResult] = []
        for candidate in candidates:
            candidate_tokens = _tokenize(candidate.text)
            if not candidate_tokens:
                results.append(RerankResult(chunk_id=candidate.chunk_id, score=0.0))
                continue

            counts = Counter(candidate_tokens)
            matched_terms = query_terms & set(candidate_tokens)
            coverage = len(matched_terms) / len(query_terms)
            density = sum(counts[t] for t in matched_terms) / len(candidate_tokens)

            score = coverage * 10.0 + density
            results.append(RerankResult(chunk_id=candidate.chunk_id, score=score))

        return sorted(results, key=lambda r: (-r.score, str(r.chunk_id)))
