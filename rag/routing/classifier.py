"""
Rule-based query router (Section 12-13). REFERENCE IMPLEMENTATION.

Deterministic, explainable, no model call: each rule is (route, signal name, regex,
weight). Scores are summed per route; compound routes are produced by explicit
combination rules, ties break by a fixed priority order, and low confidence falls back
deterministically to HYBRID_SEARCH (ACL-enforced retrieval is the safe general default).

`confidence` = top / (top + runner_up + 1): a heuristic rule score, NOT a calibrated
probability. Evolving this: an LLM or trained classifier can implement the same
`route_query` contract; the rule engine stays as the deterministic fallback.

The router only DECIDES. It never executes a route, touches data, or bypasses
authorization -- every executor re-enforces tenant + ACL itself. The security context is
carried on the decision (tenant_id from the authenticated Identity, never from the query).
Logs carry a short query hash and length, never the query text.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime

from packages.auth.identity import Identity
from packages.observability.logging import get_logger

from rag.routing.base import QueryRoute, RoutingDecision, TimeMode
from rag.routing.entities import extract_entities
from rag.routing.normalize import normalize_query
from rag.routing.time_extraction import extract_time_range

CLASSIFIER_VERSION = "rules-v1"
FALLBACK_CONFIDENCE_THRESHOLD = 0.34

_log = get_logger(component="query_router")

_DATA_NOUNS = r"(?:tickets?|incidents?|orders?|customers?|deployments?|refunds?|users?|requests?)"

# (route, signal name, pattern, weight)
_RULES: list[tuple[QueryRoute, str, re.Pattern[str], float]] = [
    (QueryRoute.ACCESS_CHECK, "can_i_access",
     re.compile(r"\b(?:can|could|may) i (?:access|see|view|read|open|download)\b"), 3.0),
    (QueryRoute.ACCESS_CHECK, "do_i_have_access",
     re.compile(r"\b(?:do i have|am i (?:allowed|permitted|authori[sz]ed))\b.*\b(?:access|permission|view|open|read|see|download|edit)\b"), 3.0),
    (QueryRoute.DOCUMENT_COMPARISON, "compare_verb",
     re.compile(r"\bcompare\b"), 2.0),
    (QueryRoute.DOCUMENT_COMPARISON, "difference_between",
     re.compile(r"\b(?:differences?|diff)\s+between\b"), 3.0),
    (QueryRoute.DOCUMENT_COMPARISON, "what_changed_between",
     re.compile(r"\bwhat\s+(?:has\s+)?changed\s+(?:between|from)\b"), 3.0),
    (QueryRoute.DOCUMENT_COMPARISON, "versions_vs",
     re.compile(r"\bv\d+\b.*\b(?:vs\.?|versus|and|to)\b.*\bv\d+\b"), 2.0),
    (QueryRoute.DOCUMENT_COMPARISON, "relative_version",
     re.compile(r"\b(?:previous|prior|older|earlier|last)\s+version\b"), 3.0),
    (QueryRoute.DOCUMENT_COMPARISON, "differ_from",
     re.compile(r"\bdiffers?\s+from\b"), 2.0),
    (QueryRoute.DOCUMENT_COMPARISON, "document_noun",
     re.compile(r"\b(?:polic(?:y|ies)|document|version|spec|contract|api)\b"), 1.0),
    (QueryRoute.SQL_ANALYTICS, "how_many",
     re.compile(rf"\bhow many\b.*\b{_DATA_NOUNS}\b"), 3.0),
    (QueryRoute.SQL_ANALYTICS, "count_total",
     re.compile(r"\b(?:count of|number of|total number|average|avg|sum of|median|percentage of)\b"), 3.0),
    (QueryRoute.SQL_ANALYTICS, "per_period",
     re.compile(r"\bper (?:day|week|month|hour|region|service|customer)\b"), 2.0),
    (QueryRoute.SQL_ANALYTICS, "trend_top_n",
     re.compile(r"\b(?:trend|top \d+|breakdown|group(?:ed)? by)\b"), 2.0),
    (QueryRoute.SQL_ANALYTICS, "data_noun",
     re.compile(rf"\b{_DATA_NOUNS}\b"), 1.0),
    (QueryRoute.INCIDENT_ANALYSIS, "incident_word",
     re.compile(r"\b(?:incidents?|outages?|postmortems?|post-mortems?|sev[ -]?[0-4]|downtime)\b"), 3.0),
    (QueryRoute.INCIDENT_ANALYSIS, "incident_state",
     re.compile(r"\b(?:unresolved|ongoing|recurring|root cause)\b"), 1.0),
    (QueryRoute.SUPPORT_ANALYSIS, "ticket_word",
     re.compile(r"\b(?:tickets?|support cases?|escalations?)\b"), 3.0),
    (QueryRoute.SUPPORT_ANALYSIS, "why_customer",
     re.compile(r"\bwhy (?:did|does|is|was)\b.*\bcustomer\b"), 3.0),
    (QueryRoute.DOCUMENT_LOOKUP, "find_document",
     re.compile(r"\b(?:find|open|show|get|fetch|pull up)\b.*\b(?:the )?(?:document|doc|file|policy document|pdf)\b"), 2.0),
    (QueryRoute.DOCUMENT_LOOKUP, "quoted_title",
     re.compile(r"[\"“][^\"”]{2,}[\"”]\s*(?:document|doc|file|policy|pdf)"), 2.0),
    (QueryRoute.LEXICAL_SEARCH, "quoted_phrase",
     re.compile(r"[\"“][^\"”]{2,}[\"”]"), 3.0),
    (QueryRoute.LEXICAL_SEARCH, "identifier",
     re.compile(r"\b[A-Z]{2,10}-\d{2,8}\b|\b(?:error|err|code)\s*[:#]?\s*\d{3,6}\b", re.IGNORECASE), 3.0),
    (QueryRoute.LEXICAL_SEARCH, "exact_phrase_words",
     re.compile(r"\b(?:exact(?:ly)?|verbatim|contains? the (?:word|phrase))\b"), 3.0),
    (QueryRoute.HYBRID_SEARCH, "search_verb",
     re.compile(r"\b(?:find|search(?: for)?|look for|locate|list|which|show me)\b"), 2.0),
    (QueryRoute.HYBRID_SEARCH, "complaining",
     re.compile(r"\b(?:complain\w*|report(?:ed|ing)?|mention\w*|related to|involving)\b"), 1.0),
    (QueryRoute.RAG_KNOWLEDGE, "question_word",
     re.compile(r"^(?:what|how|why|when|who|where|is|are|does|do|can|should|explain|describe|summari[sz]e|tell me)\b"), 1.0),
    (QueryRoute.RAG_KNOWLEDGE, "policy_topic",
     re.compile(r"\b(?:polic(?:y|ies)|procedure|guideline|process|handbook|runbook|standard|rules?)\b"), 1.5),
]  # fmt: skip

_EXPLAIN = re.compile(
    r"\b(?:explain|why|causes?|root causes?|reasons?|summari[sz]e|major causes)\b"
)
_PERIOD_COMPARE = re.compile(
    r"\b(?:this|last) (?:week|month|quarter|year)\b.*\b(?:vs\.?|versus|and|with|compared? (?:to|with))\b.*"
    r"\b(?:this|last) (?:week|month|quarter|year)\b|\bmonth over month\b|\bweek over week\b"
)

# Tie-break order, most specific/safest first.
_PRIORITY = [
    QueryRoute.ACCESS_CHECK,
    QueryRoute.RAG_PLUS_SQL,
    QueryRoute.DOCUMENT_COMPARISON,
    QueryRoute.SQL_ANALYTICS,
    QueryRoute.INCIDENT_ANALYSIS,
    QueryRoute.SUPPORT_ANALYSIS,
    QueryRoute.DOCUMENT_LOOKUP,
    QueryRoute.LEXICAL_SEARCH,
    QueryRoute.HYBRID_SEARCH,
    QueryRoute.RAG_KNOWLEDGE,
]


def _score(text: str) -> tuple[dict[QueryRoute, float], list[str]]:
    scores: dict[QueryRoute, float] = {}
    signals: list[str] = []
    for route, name, pattern, weight in _RULES:
        if pattern.search(text):
            scores[route] = scores.get(route, 0.0) + weight
            signals.append(f"{route.value}:{name}")
    return scores, signals


def route_query(
    raw_query: str, identity: Identity, *, now: datetime | None = None
) -> RoutingDecision:
    started = time.perf_counter()
    normalized = normalize_query(raw_query)
    lowered = normalized.lower()
    time_range = extract_time_range(normalized, now=now)
    entities = extract_entities(normalized)

    scores, signals = _score(lowered)

    # A comparison verb only means DOCUMENT comparison when documents/versions are the
    # subject; "compare incidents this month vs last month" is analytics, not a diff.
    doc_cmp = scores.get(QueryRoute.DOCUMENT_COMPARISON, 0.0)
    data_subject = bool(re.search(rf"\b{_DATA_NOUNS}\b|\bincidents?\b", lowered))
    period_compare = bool(_PERIOD_COMPARE.search(lowered))
    if len(entities.version_refs) >= 2:
        scores[QueryRoute.DOCUMENT_COMPARISON] = doc_cmp + 2.0
        signals.append("DOCUMENT_COMPARISON:two_version_refs")
    elif data_subject and period_compare:
        scores.pop(QueryRoute.DOCUMENT_COMPARISON, None)
        scores[QueryRoute.SQL_ANALYTICS] = scores.get(QueryRoute.SQL_ANALYTICS, 0.0) + 3.0
        signals.append("SQL_ANALYTICS:period_comparison")
    elif (
        doc_cmp < 3.0
        and "compare" in lowered
        and not re.search(r"\b(?:polic(?:y|ies)|document|version|spec|contract)\b", lowered)
    ):
        scores.pop(QueryRoute.DOCUMENT_COMPARISON, None)

    # Compound: structured metrics AND an explanation request -> RAG_PLUS_SQL.
    sql_score = scores.get(QueryRoute.SQL_ANALYTICS, 0.0)
    incident_or_support = scores.get(QueryRoute.INCIDENT_ANALYSIS, 0.0) + scores.get(
        QueryRoute.SUPPORT_ANALYSIS, 0.0
    )
    if sql_score >= 3.0 and _EXPLAIN.search(lowered):
        scores[QueryRoute.RAG_PLUS_SQL] = sql_score + 2.0
        signals.append("RAG_PLUS_SQL:metrics_plus_explanation")
    elif sql_score >= 3.0 and incident_or_support >= 3.0 and "how many" not in lowered:
        # e.g. "compare incidents this month vs last month" with no explicit "explain"
        pass
    # Incident/support words with counts ("how many tickets") stay analytics (SQL wins by
    # score and priority); pure investigation questions stay incident/support analysis.

    if not scores or not re.search(r"[a-z]{2,}", lowered):
        decision_route, confidence, reason, fallback = (
            (QueryRoute.UNKNOWN, 0.0, "no alphabetic content to classify", False)
            if not re.search(r"[a-z]{2,}", lowered)
            else (QueryRoute.HYBRID_SEARCH, 0.0, "no routing signals matched; safe default", True)
        )
    else:
        ranked = sorted(
            scores.items(),
            key=lambda kv: (-kv[1], _PRIORITY.index(kv[0]) if kv[0] in _PRIORITY else 99),
        )
        top_route, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        confidence = round(top_score / (top_score + second + 1.0), 3)
        if confidence < FALLBACK_CONFIDENCE_THRESHOLD:
            decision_route, fallback = QueryRoute.HYBRID_SEARCH, True
            reason = f"low confidence ({confidence}) for {top_route.value}; fell back to hybrid"
        else:
            decision_route, fallback = top_route, False
            reason = f"highest rule score {top_score} for {top_route.value}" + (
                f" over {ranked[1][0].value} ({second})" if len(ranked) > 1 else ""
            )

    latency_ms = (time.perf_counter() - started) * 1000
    decision = RoutingDecision(
        route=decision_route,
        confidence=confidence,
        classifier_version=CLASSIFIER_VERSION,
        reason=reason,
        latency_ms=latency_ms,
        tenant_id=identity.tenant_id,
        normalized_query=normalized,
        time_range=time_range,
        entities=entities,
        matched_signals=tuple(signals),
        fallback_used=fallback,
    )
    _log.info(
        "query_routed",
        tenant_id=str(identity.tenant_id),
        route=decision.route.value,
        confidence=decision.confidence,
        classifier_version=CLASSIFIER_VERSION,
        reason=decision.reason,
        latency_ms=round(latency_ms, 3),
        fallback_used=fallback,
        time_mode=time_range.mode.value if time_range.mode != TimeMode.NONE else None,
        query_hash=hashlib.sha256(normalized.encode()).hexdigest()[:12],
        query_length=len(normalized),
    )
    return decision
