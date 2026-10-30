"""
Prometheus metrics (Spec §60-65).

Cardinality rules (enforced by tests/unit/test_metrics.py):
- NO label may carry tenant_id, user_id, document_id, query text, filenames, or error
  messages. Labels are small closed sets: HTTP route *templates*, query routes, outcomes,
  provider/model names, Kafka topics. Per-tenant visibility comes from logs and the audit
  trail, not metric labels (tenant count is unbounded in a multi-tenant SaaS).
- Metrics are defined once, on a dedicated registry, so importing this module twice (tests)
  never raises "Duplicated timeseries" and the default process collectors stay opt-in.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

_LATENCY = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)
_SLOW = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

ALLOWED_LABEL_NAMES = frozenset(
    {
        "method", "route", "status_class", "status", "outcome", "provider", "model",
        "direction", "topic", "stage", "reason", "result", "version", "partition",
        "limit", "kind",  # bounded: token|cost and llm|embedding
    }
)  # fmt: skip


def _c(name: str, doc: str, labels: tuple[str, ...] = ()) -> Counter:
    return Counter(name, doc, labels, registry=REGISTRY)


def _h(
    name: str, doc: str, labels: tuple[str, ...] = (), buckets: tuple[float, ...] = _LATENCY
) -> Histogram:
    return Histogram(name, doc, labels, buckets=buckets, registry=REGISTRY)


def _g(name: str, doc: str, labels: tuple[str, ...] = ()) -> Gauge:
    return Gauge(name, doc, labels, registry=REGISTRY)


# --- HTTP ------------------------------------------------------------------------------
HTTP_REQUESTS = _c("ekc_http_requests_total", "HTTP requests.", ("method", "route", "status_class"))
HTTP_DURATION = _h("ekc_http_request_duration_seconds", "HTTP latency.", ("method", "route"))
HTTP_IN_FLIGHT = _g("ekc_http_requests_in_flight", "Requests being served.")

# --- Query pipeline ----------------------------------------------------------------------
QUERY_REQUESTS = _c("ekc_query_requests_total", "Queries by route and status.", ("route", "status"))
QUERY_DURATION = _h("ekc_query_duration_seconds", "Query latency.", ("route",), _SLOW)
ABSTENTIONS = _c("ekc_answer_abstentions_total", "Abstentions by reason.", ("reason",))
INJECTION_QUARANTINED = _c(
    "ekc_injection_chunks_quarantined_total", "Chunks withheld from context as likely injection."
)
OUTPUT_POLICY_VIOLATIONS = _c(
    "ekc_output_policy_violations_total", "Model outputs blocked by output policy."
)
CITATION_FAILURES = _c(
    "ekc_citation_validation_failures_total", "Model outputs rejected for bad/missing citations."
)

# --- LLM -------------------------------------------------------------------------------------
LLM_REQUESTS = _c("ekc_llm_requests_total", "LLM calls.", ("provider", "model", "outcome"))
LLM_DURATION = _h("ekc_llm_request_duration_seconds", "LLM latency.", ("provider", "model"), _SLOW)
LLM_TOKENS = _c("ekc_llm_tokens_total", "LLM tokens.", ("provider", "model", "direction"))
LLM_COST = _c(
    "ekc_llm_cost_usd_total",
    "LLM spend; only counted when a price is configured.",
    ("provider", "model"),
)
BUDGET_REJECTIONS = _c(
    "ekc_budget_rejections_total",
    "LLM calls refused because a tenant reached its daily budget.",
    ("limit",),  # token | cost
)
USAGE_RECORD_FAILURES = _c(
    "ekc_usage_record_failures_total",
    "Usage events that could not be persisted (the ledger under-counts by this many).",
    ("kind",),
)
EMBEDDING_TOKENS = _c(
    "ekc_embedding_tokens_total", "Embedding input tokens reported by the provider.", ("model",)
)
LOGIN_THROTTLED = _c(
    "ekc_login_throttled_total", "Sign-in attempts refused by the brute-force throttle.", ("kind",)
)
LOGIN_THROTTLE_ERRORS = _c(
    "ekc_login_throttle_errors_total",
    "Redis errors in the login throttle (protection degraded while non-zero).",
    ("kind",),
)
LLM_RETRIES = _c("ekc_llm_retries_total", "LLM retries.", ("provider",))

# --- Kafka / ingestion -------------------------------------------------------------------------
KAFKA_MESSAGES = _c("ekc_kafka_messages_total", "Messages by final outcome.", ("topic", "outcome"))
KAFKA_HANDLER_DURATION = _h(
    "ekc_kafka_handler_duration_seconds", "Per-message handling time.", ("topic",), _SLOW
)
KAFKA_RETRIES = _c("ekc_kafka_handler_retries_total", "In-process handler retries.", ("topic",))
KAFKA_CONSUMER_LAG = _g(
    "ekc_kafka_consumer_lag_messages", "Messages behind the log end.", ("topic", "partition")
)
OUTBOX_EVENTS = _c("ekc_outbox_relay_events_total", "Outbox relay results.", ("result",))
OUTBOX_BACKLOG = _g("ekc_outbox_unpublished_events", "Unpublished outbox rows.")
OUTBOX_OLDEST_AGE = _g(
    "ekc_outbox_oldest_unpublished_age_seconds", "Age of the oldest unpublished row."
)
INGESTION_DOCUMENTS = _c("ekc_ingestion_documents_total", "Documents processed.", ("outcome",))
INGESTION_STAGE = _h(
    "ekc_ingestion_stage_duration_seconds", "Ingestion stage time.", ("stage",), _SLOW
)

# --- Audit / config -------------------------------------------------------------------------------
AUDIT_WRITE_FAILURES = _c(
    "ekc_audit_write_failures_total", "Audit events that could not be written."
)
BUILD_INFO = _g("ekc_build_info", "Always 1; labels carry the version.", ("version",))


def status_class(status: int | None) -> str:
    return "5xx" if status is None else f"{status // 100}xx"


@contextmanager
def timed(histogram: Histogram, **labels: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        histogram.labels(**labels).observe(time.perf_counter() - start)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def all_metric_names() -> set[str]:
    """Every series name a query may legitimately reference, derived from metric TYPES (so a
    labelled metric that has not been observed yet still counts)."""
    names: set[str] = set()
    for family in REGISTRY.collect():
        names.add(family.name)
        if family.type == "counter":
            names.update({f"{family.name}_total", f"{family.name}_created"})
        elif family.type == "histogram":
            names.update({f"{family.name}_{suffix}" for suffix in ("bucket", "sum", "count")})
    return names
