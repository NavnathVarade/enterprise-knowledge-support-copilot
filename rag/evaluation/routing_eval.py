"""Routing evaluation: runs the real classifier (no database, no model)."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from packages.auth.identity import Identity

from rag.evaluation.dataset import RoutingCase
from rag.evaluation.metrics import routing_scores
from rag.evaluation.report import EvalReport
from rag.routing.classifier import CLASSIFIER_VERSION, route_query


def evaluate_routing(cases: list[RoutingCase], *, dataset: str) -> EvalReport:
    tenant = uuid.uuid4()
    pairs: list[tuple[str, str]] = []
    failures = []
    for c in cases:
        identity = Identity(
            uuid.uuid4(), tenant, "eval@example.com", frozenset(c.roles), frozenset()
        )
        decision = route_query(c.query, identity)
        pairs.append((c.expected_route.value, decision.route.value))
        if decision.route is not c.expected_route:
            failures.append(
                {
                    "id": c.id,
                    "detail": f"expected {c.expected_route.value}, got {decision.route.value}: {c.query}",
                }
            )
    s = routing_scores(pairs)
    return EvalReport(
        suite="routing",
        dataset=dataset,
        n_cases=s.n,
        metrics={
            "accuracy": s.accuracy,
            "correct": s.correct,
            "accuracy_ci95_low": s.accuracy_ci95[0] if s.accuracy_ci95 else None,
            "accuracy_ci95_high": s.accuracy_ci95[1] if s.accuracy_ci95 else None,
            "per_route": s.per_route,
            "confusion": s.confusion,
        },
        measured_with={"classifier": CLASSIFIER_VERSION},
        caveats=[
            "Dev-quality set: rules were written against similar phrasings; optimistic, "
            "not a held-out estimate.",
            "Small n: see the 95% Wilson interval.",
        ],
        failures=failures,
    )


__all__ = ["evaluate_routing", "asdict"]
