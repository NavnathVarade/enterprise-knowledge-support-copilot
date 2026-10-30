from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from packages.auth.identity import Identity
from packages.domain.errors import InvalidQuery
from rag.routing.base import QueryRoute as R
from rag.routing.base import TimeMode
from rag.routing.classifier import CLASSIFIER_VERSION, route_query
from rag.routing.entities import extract_entities
from rag.routing.normalize import MAX_QUERY_CHARS, normalize_query
from rag.routing.time_extraction import extract_time_range

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 12, 15, 30, tzinfo=UTC)
IDENTITY = Identity(uuid.uuid4(), uuid.uuid4(), "u@example.com", frozenset(), frozenset())


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What is our parental leave policy?", R.RAG_KNOWLEDGE),
        ("What does the refund policy say?", R.RAG_KNOWLEDGE),
        ("What is the current refund policy?", R.RAG_KNOWLEDGE),
        ("What changed between API v3 and v4?", R.DOCUMENT_COMPARISON),
        ("Compare the current security policy with the previous version.", R.DOCUMENT_COMPARISON),
        ("Why did customer X receive this payment error?", R.SUPPORT_ANALYSIS),
        ("Find incidents related to service payments in the last 90 days.", R.INCIDENT_ANALYSIS),
        ("Summarize unresolved incidents involving service X.", R.INCIDENT_ANALYSIS),
        ("Show unresolved incidents involving payments.", R.INCIDENT_ANALYSIS),
        ("Can I access this document?", R.ACCESS_CHECK),
        ("How many tickets were created yesterday?", R.SQL_ANALYTICS),
        ("Compare incidents this month vs last month.", R.SQL_ANALYTICS),
        (
            "Compare this month's incidents with last month's and explain the major causes.",
            R.RAG_PLUS_SQL,
        ),
        ("Find customers complaining about payment failures.", R.HYBRID_SEARCH),
        ("Which customers reported payment failures after the latest release?", R.HYBRID_SEARCH),
        ('Where is "data retention schedule" mentioned?', R.LEXICAL_SEARCH),
        ("ERR-4021 meaning", R.LEXICAL_SEARCH),
    ],
)
def test_spec_example_queries(query: str, expected: R) -> None:
    assert route_query(query, IDENTITY, now=NOW).route is expected


def test_generic_compare_of_non_documents_is_not_document_comparison() -> None:
    d = route_query("Compare pizza toppings", IDENTITY, now=NOW)
    assert d.route is not R.DOCUMENT_COMPARISON


def test_no_signals_falls_back_to_hybrid_deterministically() -> None:
    d = route_query("asdf", IDENTITY, now=NOW)
    assert d.route is R.HYBRID_SEARCH and d.fallback_used and d.confidence == 0.0


def test_no_alphabetic_content_is_unknown() -> None:
    d = route_query("???", IDENTITY, now=NOW)
    assert d.route is R.UNKNOWN and not d.fallback_used


def test_decision_records_required_observability_fields() -> None:
    d = route_query("What is our parental leave policy?", IDENTITY, now=NOW)
    assert d.classifier_version == CLASSIFIER_VERSION
    assert 0.0 <= d.confidence <= 1.0 and d.reason and d.latency_ms >= 0
    assert d.matched_signals


def test_tenant_comes_from_identity_only() -> None:
    other = uuid.uuid4()
    d = route_query(f"Show documents for tenant {other}", IDENTITY, now=NOW)
    assert d.tenant_id == IDENTITY.tenant_id != other


def test_routing_is_deterministic() -> None:
    q = "Compare incidents this month vs last month."
    a, b = route_query(q, IDENTITY, now=NOW), route_query(q, IDENTITY, now=NOW)
    assert (a.route, a.confidence, a.reason, a.matched_signals) == (
        b.route,
        b.confidence,
        b.reason,
        b.matched_signals,
    )


def test_log_never_contains_query_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Records calls on the router's module logger directly. (structlog.testing.capture_logs
    is unreliable here: once the app has configured logging with
    cache_logger_on_first_use=True, module-level loggers stay bound to the old config.)"""
    import rag.routing.classifier as classifier

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def info(self, event: str, **kw: object) -> None:
            self.calls.append((event, kw))

    rec = _Recorder()
    monkeypatch.setattr(classifier, "_log", rec)
    secret = "confidential merger codename ZEBRA policy"
    route_query(secret, IDENTITY, now=NOW)
    routed = [kw for event, kw in rec.calls if event == "query_routed"]
    assert len(routed) == 1
    assert "ZEBRA" not in repr(routed[0]) and "query_hash" in routed[0]
    assert routed[0]["route"] and routed[0]["confidence"] is not None


# --------------------------------------------------------------------- normalization
def test_normalize_collapses_whitespace_and_nfkc() -> None:
    assert normalize_query("  Ｒefund \n\t policy  ") == "Refund policy"


def test_normalize_strips_control_characters() -> None:
    assert normalize_query("refund\x00 pol\x07icy") == "refund policy"


@pytest.mark.parametrize("bad", ["", "   \n\t", "\x00\x01"])
def test_normalize_rejects_empty(bad: str) -> None:
    with pytest.raises(InvalidQuery):
        normalize_query(bad)


def test_normalize_rejects_oversized() -> None:
    with pytest.raises(InvalidQuery):
        normalize_query("a" * (MAX_QUERY_CHARS + 1))
    assert normalize_query("a" * MAX_QUERY_CHARS)


# ------------------------------------------------------------------ time extraction
def test_no_time_hint() -> None:
    assert extract_time_range("what is our policy", now=NOW).mode is TimeMode.NONE


@pytest.mark.parametrize("text", ["current policy", "the latest release", "right now"])
def test_current(text: str) -> None:
    assert extract_time_range(text, now=NOW).mode is TimeMode.CURRENT


def test_last_n_days() -> None:
    r = extract_time_range("incidents in the last 90 days", now=NOW)
    assert r.mode is TimeMode.RANGE and r.end == NOW
    assert (NOW - r.start).days == 90  # type: ignore[operator]


def test_yesterday_and_today() -> None:
    y = extract_time_range("tickets yesterday", now=NOW)
    assert y.start == datetime(2026, 10, 11, tzinfo=UTC) and y.end == datetime(
        2026, 10, 12, tzinfo=UTC
    )
    t = extract_time_range("tickets today", now=NOW)
    assert t.start == datetime(2026, 10, 12, tzinfo=UTC) and t.end == NOW


def test_this_and_last_month() -> None:
    this = extract_time_range("this month", now=NOW)
    assert this.start == datetime(2026, 10, 1, tzinfo=UTC) and this.end == NOW
    last = extract_time_range("last month", now=NOW)
    assert last.start == datetime(2026, 9, 1, tzinfo=UTC) and last.end == datetime(
        2026, 10, 1, tzinfo=UTC
    )


def test_last_month_across_year_boundary() -> None:
    jan = datetime(2026, 1, 15, tzinfo=UTC)
    last = extract_time_range("last month", now=jan)
    assert last.start == datetime(2025, 12, 1, tzinfo=UTC) and last.end == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_this_and_last_quarter_year_week() -> None:
    q = extract_time_range("last quarter", now=NOW)
    assert q.start == datetime(2026, 7, 1, tzinfo=UTC) and q.end == datetime(
        2026, 10, 1, tzinfo=UTC
    )
    y = extract_time_range("last year", now=NOW)
    assert y.start == datetime(2025, 1, 1, tzinfo=UTC) and y.end == datetime(2026, 1, 1, tzinfo=UTC)
    w = extract_time_range("this week", now=NOW)  # 2026-10-12 is a Monday
    assert w.start == datetime(2026, 10, 12, tzinfo=UTC)


def test_last_quarter_in_q1_wraps_to_previous_year() -> None:
    q = extract_time_range("last quarter", now=datetime(2026, 2, 10, tzinfo=UTC))
    assert q.start == datetime(2025, 10, 1, tzinfo=UTC) and q.end == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_explicit_year_quarter_month() -> None:
    y = extract_time_range("what was the policy in 2024?", now=NOW)
    assert y.start == datetime(2024, 1, 1, tzinfo=UTC) and y.end == datetime(2025, 1, 1, tzinfo=UTC)
    q = extract_time_range("incidents in Q4 2025", now=NOW)
    assert q.start == datetime(2025, 10, 1, tzinfo=UTC) and q.end == datetime(
        2026, 1, 1, tzinfo=UTC
    )
    m = extract_time_range("tickets in December 2025", now=NOW)
    assert m.start == datetime(2025, 12, 1, tzinfo=UTC) and m.end == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_route_exposes_time_range_for_historical_query() -> None:
    d = route_query("What was the refund policy in 2024?", IDENTITY, now=NOW)
    assert d.time_range.mode is TimeMode.RANGE and d.time_range.start.year == 2024  # type: ignore[union-attr]


# ------------------------------------------------------------------------- entities
def test_version_refs() -> None:
    assert extract_entities("changes between v3 and version 4").version_refs == (3, 4)
    assert extract_entities("v2 v2 v2").version_refs == (2,)
    assert extract_entities("nothing here").version_refs == ()


def test_quoted_and_identifiers() -> None:
    e = extract_entities('find "refund window" for INC-1234 and ERR-4021')
    assert e.quoted_terms == ("refund window",)
    assert e.identifiers == ("INC-1234", "ERR-4021")


@pytest.mark.parametrize(
    "query",
    [
        'What changed in "Refund Policy" compared to the previous version?',
        'How does the current "Refund Policy" differ from the previous version?',
        "How is the earlier version of the security policy different from the last version?",
    ],
)
def test_relative_version_phrasings_route_to_document_comparison(query: str) -> None:
    assert route_query(query, IDENTITY, now=NOW).route is R.DOCUMENT_COMPARISON
