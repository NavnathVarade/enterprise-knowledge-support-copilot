from __future__ import annotations

import uuid

import pytest
from rag.reranking.base import RerankCandidate
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker


def _candidate(text: str) -> RerankCandidate:
    return RerankCandidate(chunk_id=uuid.uuid4(), text=text)


@pytest.mark.unit
async def test_higher_coverage_ranks_above_lower_coverage() -> None:
    reranker = LexicalOverlapReranker()
    full_match = _candidate("The refund policy allows returns within thirty days.")
    partial_match = _candidate("Refunds are handled by a separate team entirely.")

    results = await reranker.rerank("refund policy returns days", [partial_match, full_match])

    assert results[0].chunk_id == full_match.chunk_id
    assert results[0].score > results[1].score


@pytest.mark.unit
async def test_candidate_with_no_matching_terms_scores_zero() -> None:
    reranker = LexicalOverlapReranker()
    unrelated = _candidate("The weather today is sunny and warm.")
    results = await reranker.rerank("refund policy", [unrelated])
    assert results[0].score == 0.0


@pytest.mark.unit
async def test_empty_query_scores_everything_zero() -> None:
    reranker = LexicalOverlapReranker()
    candidates = [_candidate("some content"), _candidate("other content")]
    results = await reranker.rerank("", candidates)
    assert all(r.score == 0.0 for r in results)
    assert len(results) == 2


@pytest.mark.unit
async def test_empty_candidate_list_returns_empty() -> None:
    reranker = LexicalOverlapReranker()
    assert await reranker.rerank("query", []) == []


@pytest.mark.unit
async def test_candidate_with_empty_text_scores_zero_not_error() -> None:
    reranker = LexicalOverlapReranker()
    results = await reranker.rerank("query", [_candidate("")])
    assert results[0].score == 0.0


@pytest.mark.unit
async def test_result_is_sorted_descending() -> None:
    reranker = LexicalOverlapReranker()
    candidates = [
        _candidate("refund"),
        _candidate("refund policy returns days purchase"),
        _candidate("nothing relevant here"),
    ]
    results = await reranker.rerank("refund policy returns days purchase", candidates)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.unit
async def test_density_breaks_ties_between_equal_coverage() -> None:
    """Two candidates both mention the single query term once, but the shorter one
    (higher density) should score at least as high."""
    reranker = LexicalOverlapReranker()
    dense = _candidate("refund")
    sparse = _candidate("refund " + "filler word " * 50)

    results = await reranker.rerank("refund", [sparse, dense])
    scores_by_id = {r.chunk_id: r.score for r in results}
    assert scores_by_id[dense.chunk_id] > scores_by_id[sparse.chunk_id]


@pytest.mark.unit
async def test_result_never_contains_ids_outside_candidates() -> None:
    reranker = LexicalOverlapReranker()
    candidates = [_candidate("a"), _candidate("b")]
    results = await reranker.rerank("a b", candidates)
    candidate_ids = {c.chunk_id for c in candidates}
    assert all(r.chunk_id in candidate_ids for r in results)


@pytest.mark.unit
async def test_scoring_is_deterministic() -> None:
    reranker = LexicalOverlapReranker()
    candidates = [_candidate("refund policy"), _candidate("something else")]
    r1 = await reranker.rerank("refund", candidates)
    r2 = await reranker.rerank("refund", candidates)
    assert r1 == r2
