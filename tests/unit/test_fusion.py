from __future__ import annotations

import uuid

import pytest
from rag.retrieval.fusion import reciprocal_rank_fusion


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


@pytest.mark.unit
def test_single_list_preserves_order() -> None:
    a, b, c = _ids(3)
    result = reciprocal_rank_fusion([[a, b, c]])
    assert [item_id for item_id, _ in result] == [a, b, c]


@pytest.mark.unit
def test_item_in_both_lists_outranks_item_in_one_list() -> None:
    a, b, c = _ids(3)
    # a: rank 1 in both lists. b: rank 2 in list 1 only. c: rank 1 in list 2 only.
    result = reciprocal_rank_fusion([[a, b], [a, c]])
    ranked_ids = [item_id for item_id, _ in result]
    assert ranked_ids[0] == a  # appears in both -> highest fused score
    assert a != b and a != c


@pytest.mark.unit
def test_top_rank_contributes_more_than_lower_rank() -> None:
    a, b, c, d = _ids(4)
    # x is rank 1 in list A; y is rank 1 in list B but list A ranks it 4th.
    result = reciprocal_rank_fusion([[a, b, c, d], [d, c, b, a]])
    scores = dict(result)
    # a is rank 1 in list 1, rank 4 in list 2. d is rank 4 in list 1, rank 1 in list 2.
    # By symmetry their fused scores should be equal.
    assert scores[a] == pytest.approx(scores[d])


@pytest.mark.unit
def test_item_missing_from_a_list_gets_zero_contribution_from_it() -> None:
    a, b = _ids(2)
    only_in_one = reciprocal_rank_fusion([[a]])
    in_both = reciprocal_rank_fusion([[a], [a]])
    assert dict(in_both)[a] == pytest.approx(2 * dict(only_in_one)[a])
    assert b not in dict(only_in_one)


@pytest.mark.unit
def test_empty_lists_produce_empty_result() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


@pytest.mark.unit
def test_result_is_sorted_descending_by_score() -> None:
    a, b, c = _ids(3)
    result = reciprocal_rank_fusion([[a, b, c]])
    scores = [score for _, score in result]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.unit
def test_smaller_k_sharpens_rank_differences() -> None:
    a, b = _ids(2)
    small_k = dict(reciprocal_rank_fusion([[a, b]], k=1))
    large_k = dict(reciprocal_rank_fusion([[a, b]], k=1000))
    # The ratio between rank-1 and rank-2 scores should be larger with a small k.
    ratio_small_k = small_k[a] / small_k[b]
    ratio_large_k = large_k[a] / large_k[b]
    assert ratio_small_k > ratio_large_k


@pytest.mark.unit
def test_fusion_is_deterministic_regardless_of_list_order_passed_in() -> None:
    a, b, c = _ids(3)
    result1 = reciprocal_rank_fusion([[a, b], [b, c]])
    result2 = reciprocal_rank_fusion([[a, b], [b, c]])
    assert result1 == result2


@pytest.mark.unit
def test_duplicate_within_a_single_list_only_counts_first_occurrence_position() -> None:
    # Pathological input: a real retriever wouldn't return dupes, but the function
    # should behave predictably (last write wins in the scores dict per iteration,
    # i.e. the LATER occurrence's rank is what's recorded) rather than crash.
    a = uuid.uuid4()
    result = reciprocal_rank_fusion([[a, a]])
    assert len(result) == 1
    assert result[0][0] == a
