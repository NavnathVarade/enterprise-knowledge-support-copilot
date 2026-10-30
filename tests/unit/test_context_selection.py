from __future__ import annotations

import uuid

import pytest
from rag.context.selection import CandidateInfo, select_for_context

pytestmark = pytest.mark.unit


def _c(
    doc: uuid.UUID,
    h: str,
    tokens: int = 10,
    score: float = 1.0,
    version: int = 1,
) -> CandidateInfo:
    return CandidateInfo(
        chunk_id=uuid.uuid4(),
        document_id=doc,
        content_hash=h,
        token_count=tokens,
        rerank_score=score,
        document_version=version,
    )


def test_empty_input() -> None:
    r = select_for_context([], max_tokens=100, max_chunks_per_document=3)
    assert r.selected_ids == []
    assert r.total_tokens == 0
    assert not r.truncated_by_budget


def test_order_preserved() -> None:
    cs = [_c(uuid.uuid4(), f"h{i}") for i in range(4)]
    r = select_for_context(cs, max_tokens=100, max_chunks_per_document=3)
    assert r.selected_ids == [c.chunk_id for c in cs]


def test_dedup_by_content_hash_across_documents() -> None:
    a, b = _c(uuid.uuid4(), "same"), _c(uuid.uuid4(), "same")
    r = select_for_context([a, b], max_tokens=100, max_chunks_per_document=3)
    assert r.selected_ids == [a.chunk_id]
    assert r.dropped_duplicate_count == 1


def test_per_document_cap() -> None:
    d = uuid.uuid4()
    cs = [_c(d, f"h{i}") for i in range(5)]
    r = select_for_context(cs, max_tokens=1000, max_chunks_per_document=2)
    assert r.selected_ids == [cs[0].chunk_id, cs[1].chunk_id]
    assert r.dropped_source_diversity_count == 3


def test_token_budget_never_exceeded_and_flagged() -> None:
    cs = [_c(uuid.uuid4(), f"h{i}", tokens=40) for i in range(3)]
    r = select_for_context(cs, max_tokens=100, max_chunks_per_document=3)
    assert r.total_tokens == 80
    assert r.truncated_by_budget
    assert r.dropped_token_budget_count == 1


def test_oversized_chunk_skipped_but_smaller_later_chunk_fits() -> None:
    big, small = _c(uuid.uuid4(), "big", tokens=500), _c(uuid.uuid4(), "small", tokens=5)
    r = select_for_context([big, small], max_tokens=100, max_chunks_per_document=3)
    assert r.selected_ids == [small.chunk_id]
    assert r.truncated_by_budget


def test_exact_budget_fits() -> None:
    cs = [_c(uuid.uuid4(), "a", tokens=50), _c(uuid.uuid4(), "b", tokens=50)]
    r = select_for_context(cs, max_tokens=100, max_chunks_per_document=3)
    assert r.total_tokens == 100
    assert not r.truncated_by_budget


def test_version_consistency_first_seen_version_wins() -> None:
    d = uuid.uuid4()
    v3, v2, v3b = _c(d, "a", version=3), _c(d, "b", version=2), _c(d, "c", version=3)
    r = select_for_context([v3, v2, v3b], max_tokens=100, max_chunks_per_document=5)
    assert r.selected_ids == [v3.chunk_id, v3b.chunk_id]
    assert r.dropped_version_inconsistent_count == 1


def test_version_pin_held_even_if_pinning_chunk_dropped() -> None:
    d, other = uuid.uuid4(), uuid.uuid4()
    first = _c(other, "dup")
    pin = _c(d, "dup", version=3)  # dropped as duplicate, but still pins v3
    stale = _c(d, "x", version=2)
    r = select_for_context([first, pin, stale], max_tokens=100, max_chunks_per_document=5)
    assert r.selected_ids == [first.chunk_id]
    assert r.dropped_version_inconsistent_count == 1


def test_conflicting_documents_both_survive() -> None:
    a, b = _c(uuid.uuid4(), "30 days"), _c(uuid.uuid4(), "14 days")
    r = select_for_context([a, b], max_tokens=100, max_chunks_per_document=3)
    assert len(r.selected_ids) == 2


def test_counters_sum_to_input() -> None:
    d = uuid.uuid4()
    cs = [_c(d, "a"), _c(d, "a"), _c(d, "b"), _c(d, "c"), _c(d, "d", tokens=999)]
    r = select_for_context(cs, max_tokens=100, max_chunks_per_document=2)
    dropped = (
        r.dropped_duplicate_count
        + r.dropped_source_diversity_count
        + r.dropped_token_budget_count
        + r.dropped_version_inconsistent_count
    )
    assert len(r.selected_ids) + dropped == len(cs)


@pytest.mark.parametrize("kwargs", [{"max_tokens": 0}, {"max_chunks_per_document": 0}])
def test_invalid_limits_rejected(kwargs: dict[str, int]) -> None:
    params = {"max_tokens": 10, "max_chunks_per_document": 1} | kwargs
    with pytest.raises(ValueError):
        select_for_context([], **params)


def test_does_not_mutate_input() -> None:
    cs = [_c(uuid.uuid4(), "a"), _c(uuid.uuid4(), "a")]
    snapshot = list(cs)
    select_for_context(cs, max_tokens=100, max_chunks_per_document=1)
    assert cs == snapshot
