from __future__ import annotations

import math

import pytest
from rag.evaluation.metrics import (
    abstention_scores,
    lexical_support,
    mean,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    routing_scores,
    wilson_interval,
)

pytestmark = pytest.mark.unit


def test_recall_precision_mrr_hand_computed() -> None:
    ranked = ["a", "x", "b", "y"]
    rel = {"a", "b", "c"}
    assert recall_at_k(ranked, rel, 3) == pytest.approx(2 / 3)
    assert recall_at_k(ranked, rel, 1) == pytest.approx(1 / 3)
    assert precision_at_k(ranked, rel, 4) == 0.5
    assert precision_at_k(["a"], rel, 4) == 0.25  # denominator is k, not len(ranked)
    assert reciprocal_rank(["x", "y", "b"], rel) == pytest.approx(1 / 3)
    assert reciprocal_rank(["x"], rel) == 0.0


def test_no_labels_means_none_not_a_score() -> None:
    assert recall_at_k(["a"], set(), 3) is None
    assert reciprocal_rank(["a"], set()) is None
    assert ndcg_at_k(["a"], set(), 3) is None
    assert mean([None, None]) is None
    assert mean([1.0, None, 0.0]) == 0.5


def test_ndcg_binary() -> None:
    # relevant at ranks 1 and 3, two relevant total
    got = ndcg_at_k(["a", "x", "b"], {"a", "b"}, 3)
    dcg = 1 / math.log2(2) + 1 / math.log2(4)
    ideal = 1 / math.log2(2) + 1 / math.log2(3)
    assert got == pytest.approx(dcg / ideal)
    assert ndcg_at_k(["a", "b"], {"a", "b"}, 5) == pytest.approx(1.0)
    assert ndcg_at_k(["a", "a", "a"], {"a"}, 3) == pytest.approx(1.0)  # duplicates count once


def test_percentile_nearest_rank() -> None:
    v = [15.0, 20.0, 35.0, 40.0, 50.0]
    assert percentile(v, 50) == 35.0
    assert percentile(v, 95) == 50.0
    assert percentile([], 50) is None
    with pytest.raises(ValueError):
        percentile(v, 101)


def test_wilson_interval_properties() -> None:
    lo, hi = wilson_interval(8, 10) or (0, 0)
    assert 0.49 < lo < 0.50 and 0.94 < hi < 0.95
    assert wilson_interval(0, 0) is None
    lo0, hi0 = wilson_interval(0, 5) or (1, 0)
    assert lo0 == 0.0 and hi0 > 0.0  # small n is never "exactly 0%"
    narrow = wilson_interval(800, 1000) or (0, 0)
    assert (narrow[1] - narrow[0]) < (hi - lo)


def test_routing_scores_confusion_and_per_route() -> None:
    s = routing_scores([("A", "A"), ("A", "B"), ("B", "B"), ("B", "B")])
    assert s.n == 4 and s.correct == 3 and s.accuracy == 0.75
    assert s.confusion == {"A": {"A": 1, "B": 1}, "B": {"B": 2}}
    assert s.per_route["B"]["precision"] == pytest.approx(2 / 3)
    assert s.per_route["A"]["recall"] == 0.5
    assert routing_scores([]).accuracy is None


def test_abstention_scores() -> None:
    s = abstention_scores([(True, True), (True, False), (False, False), (False, True)])
    assert s.answer_rate_on_answerable == 0.5
    assert s.abstain_rate_on_unanswerable == 0.5
    assert abstention_scores([(True, True)]).abstain_rate_on_unanswerable is None


def test_lexical_support_catches_unrelated_but_not_negation() -> None:
    src = "Customers may request a refund within 30 days of purchase."
    assert lexical_support("Refunds within 30 days", src) == pytest.approx(3 / 4)  # no stemming
    assert (lexical_support("Parental leave is sixteen weeks", src) or 0) < 0.2
    # Documented blind spot: reversed meaning scores high.
    assert (lexical_support("Customers may not request a refund within 30 days", src) or 0) >= 0.8
    assert lexical_support("the of a", src) is None
