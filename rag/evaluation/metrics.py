"""
Pure evaluation metrics (Spec §28). No database, no model, no clock: every function is a
deterministic computation over already-collected results, so each can be unit-tested
against hand-computed values.

Conventions:
- A metric over an empty case list returns `None`, never 0.0 or 1.0: "no data" must not
  look like a score.
- Retrieval relevance is binary and defined by the dataset's labeled relevant ids.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float | None:
    """Fraction of relevant ids found in the top k. None if there are no relevant ids."""
    if not relevant:
        return None
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float | None:
    """Fraction of the top k that is relevant. The denominator is k (not len(ranked))."""
    if k <= 0:
        return None
    return len([r for r in ranked[:k] if r in relevant]) / k


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float | None:
    if not relevant:
        return None
    for i, r in enumerate(ranked, start=1):
        if r in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float | None:
    """Binary-relevance nDCG@k with log2 discount; duplicates in `ranked` count once."""
    if not relevant:
        return None
    seen: set[str] = set()
    dcg = 0.0
    for i, r in enumerate(ranked[:k], start=1):
        if r in relevant and r not in seen:
            dcg += 1.0 / math.log2(i + 1)
        seen.add(r)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else None


def mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, q in [0, 100]. Documented method; no interpolation."""
    if not values:
        return None
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion; reported next to every accuracy so a
    small evaluation set cannot masquerade as a precise number."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True)
class RoutingScores:
    n: int
    correct: int
    accuracy: float | None
    accuracy_ci95: tuple[float, float] | None
    per_route: dict[str, dict[str, float | int | None]]  # precision / recall / support
    confusion: dict[str, dict[str, int]]  # expected -> predicted -> count


def routing_scores(pairs: Sequence[tuple[str, str]]) -> RoutingScores:
    """`pairs` are (expected, predicted)."""
    n = len(pairs)
    correct = sum(1 for e, p in pairs if e == p)
    confusion: dict[str, dict[str, int]] = {}
    for e, p in pairs:
        confusion.setdefault(e, {}).setdefault(p, 0)
        confusion[e][p] += 1
    labels = sorted({e for e, _ in pairs} | {p for _, p in pairs})
    per_route: dict[str, dict[str, float | int | None]] = {}
    for label in labels:
        tp = sum(1 for e, p in pairs if e == label and p == label)
        predicted = sum(1 for _, p in pairs if p == label)
        support = sum(1 for e, _ in pairs if e == label)
        per_route[label] = {
            "precision": tp / predicted if predicted else None,
            "recall": tp / support if support else None,
            "support": support,
        }
    return RoutingScores(
        n=n,
        correct=correct,
        accuracy=correct / n if n else None,
        accuracy_ci95=wilson_interval(correct, n),
        per_route=per_route,
        confusion=confusion,
    )


@dataclass(frozen=True)
class AbstentionScores:
    n: int
    answerable: int
    unanswerable: int
    answered_when_answerable: int
    abstained_when_unanswerable: int
    answer_rate_on_answerable: float | None  # recall of answering
    abstain_rate_on_unanswerable: float | None  # 1 - hallucination-risk rate


def abstention_scores(pairs: Sequence[tuple[bool, bool]]) -> AbstentionScores:
    """`pairs` are (case_is_answerable, system_answered)."""
    ans = [(a, s) for a, s in pairs if a]
    unans = [(a, s) for a, s in pairs if not a]
    answered = sum(1 for _, s in ans if s)
    abstained = sum(1 for _, s in unans if not s)
    return AbstentionScores(
        n=len(pairs),
        answerable=len(ans),
        unanswerable=len(unans),
        answered_when_answerable=answered,
        abstained_when_unanswerable=abstained,
        answer_rate_on_answerable=answered / len(ans) if ans else None,
        abstain_rate_on_unanswerable=abstained / len(unans) if unans else None,
    )


_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "by",
        "with",
        "as",
        "at",
        "it",
        "its",
        "this",
        "that",
        "from",
        "can",
        "may",
        "will",
        "has",
        "have",
        "had",
        "not",
        "no",
    ]
)


def _content_words(text: str) -> Counter[str]:
    return Counter(w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1)


def lexical_support(claim: str, source: str) -> float | None:
    """Share of the claim's content words that appear in the source (0..1).

    A cheap HEURISTIC for "is this sentence grounded in what it cites". It catches a
    citation pointing at unrelated text; it cannot catch a paraphrase that reverses
    meaning ("is allowed" vs "is not allowed") -- negation is a content-word miss only by
    accident. Use a human or an NLI/LLM judge for real faithfulness. None if the claim has
    no content words.
    """
    words = set(_content_words(claim))
    if not words:
        return None
    src = set(_content_words(source))
    return len(words & src) / len(words)
