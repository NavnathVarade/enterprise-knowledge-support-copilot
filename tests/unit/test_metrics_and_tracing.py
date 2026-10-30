from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from packages.config.settings import ObservabilitySettings
from packages.kafka.runner import report_consumer_lag
from packages.observability import metrics as m
from packages.observability.tracing import (
    configure_tracing,
    context_from_traceparent,
    current_traceparent,
    safe_attributes,
    shutdown_tracing,
    span,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_LABELS = {"tenant_id", "tenant", "user_id", "user", "document_id", "query", "email",
                    "filename", "error", "message", "path", "request_id", "trace_id"}  # fmt: skip


def test_every_metric_is_namespaced_and_has_only_bounded_labels() -> None:
    families = list(m.REGISTRY.collect())
    assert families
    for fam in families:
        assert fam.name.startswith("ekc_"), fam.name
    for collector in list(m.REGISTRY._collector_to_names):  # noqa: SLF001
        for label in getattr(collector, "_labelnames", ()):
            assert label not in FORBIDDEN_LABELS, (collector, label)
            assert label in m.ALLOWED_LABEL_NAMES, (collector, label)


def test_status_class_and_render() -> None:
    assert m.status_class(204) == "2xx" and m.status_class(503) == "5xx"
    assert m.status_class(None) == "5xx"  # no response start => treated as a server failure
    body, ctype = m.render_metrics()
    assert b"ekc_http_requests_total" in body and "text/plain" in ctype


def _alert_rules() -> list[dict[str, object]]:
    doc = yaml.safe_load((ROOT / "monitoring/prometheus/alerts.yaml").read_text())
    return [r for g in doc["groups"] for r in g["rules"]]


def _dashboard_exprs() -> list[str]:
    d = json.loads((ROOT / "monitoring/grafana/ekc-overview.json").read_text())
    return [t["expr"] for p in d["panels"] for t in p["targets"]]


def _check_expr(expr: str) -> None:
    names = m.all_metric_names()
    for metric in re.findall(r"\bekc_[a-z0-9_]+", expr):
        assert metric in names, f"{metric} is not an exported metric (in: {expr.strip()[:80]})"
    for selector in re.findall(r"\{([^}]*)\}", expr):
        for label in re.findall(r"([a-z_]+)\s*(?:=~|!=|=|!~)", selector):
            assert label in m.ALLOWED_LABEL_NAMES, f"unknown label {label}"
    for label in re.findall(r"by \(([^)]*)\)", expr):
        for part in (x.strip() for x in label.split(",")):
            assert part == "le" or part in m.ALLOWED_LABEL_NAMES, f"unknown label {part}"


def test_alert_rules_are_well_formed_and_reference_real_metrics() -> None:
    rules = _alert_rules()
    assert len(rules) >= 12
    names = [r["alert"] for r in rules]
    assert len(set(names)) == len(names)
    for r in rules:
        assert r["expr"] and r["labels"]["severity"] in {"page", "ticket"}  # type: ignore[index]
        assert r["annotations"]["summary"] and r["annotations"]["runbook"]  # type: ignore[index]
        _check_expr(str(r["expr"]))


def test_every_alert_has_a_runbook_section() -> None:
    doc = (ROOT / "docs/operations/PHASE_29_OBSERVABILITY.md").read_text()
    for r in _alert_rules():
        anchor = str(r["annotations"]["runbook"]).split("#")[1]  # type: ignore[index]
        assert f"### {r['alert']}" in doc and anchor == str(r["alert"]).lower()


def test_dashboard_queries_reference_real_metrics() -> None:
    exprs = _dashboard_exprs()
    assert len(exprs) >= 12
    for e in exprs:
        _check_expr(e)


def test_safe_attributes_drop_content_and_truncate() -> None:
    out = safe_attributes(
        {"prompt": "secret", "query": "what is the salary", "email": "a@b.c", "count": 3,
         "ok": True, "route": "RAG_KNOWLEDGE", "blob": "x" * 500, "none": None}
    )  # fmt: skip
    assert out == {"count": 3, "ok": True, "route": "RAG_KNOWLEDGE", "blob": "<redacted:long>"}


@pytest.fixture
def exporter():  # type: ignore[no-untyped-def]
    exp = InMemorySpanExporter()
    configure_tracing(
        ObservabilitySettings(), environment="test", extra_processors=[SimpleSpanProcessor(exp)]
    )
    yield exp
    shutdown_tracing()


def test_spans_nest_record_errors_and_never_export_sensitive_keys(exporter) -> None:  # type: ignore[no-untyped-def]
    with (
        span("outer", route="X", query="SECRET QUERY TEXT"),
        pytest.raises(ValueError),
        span("inner"),
    ):
        raise ValueError("boom")
    inner, outer = exporter.get_finished_spans()
    assert inner.parent is not None and inner.parent.span_id == outer.context.span_id
    assert inner.status.status_code.name == "ERROR"
    assert "query" not in outer.attributes and outer.attributes["route"] == "X"
    assert "SECRET QUERY TEXT" not in str([s.attributes for s in (inner, outer)])


def test_traceparent_round_trips_into_one_trace(exporter) -> None:  # type: ignore[no-untyped-def]
    with span("api") as api:
        tp = current_traceparent()
    assert tp and tp.startswith("00-")
    with span("worker", parent=context_from_traceparent(tp)) as worker:
        pass
    assert worker.get_span_context().trace_id == api.get_span_context().trace_id
    assert context_from_traceparent(None) is None


def test_no_provider_means_noop_spans_without_errors() -> None:
    shutdown_tracing()
    with span("anything", a=1):
        assert current_traceparent() is None


class _TP:
    def __init__(self, topic: str, partition: int, offset: int = -1001) -> None:
        self.topic, self.partition, self.offset = topic, partition, offset


class _LagClient:
    def assignment(self) -> list[_TP]:
        return [_TP("t", 0), _TP("t", 1), _TP("other", 0)]

    def committed(self, partitions: list[_TP], timeout: float = 0) -> list[_TP]:
        return [_TP("t", 0, 90), _TP("t", 1, -1001)]  # partition 1: nothing committed yet

    def get_watermark_offsets(self, p: _TP, timeout: float = 0) -> tuple[int, int]:
        return (10, 100) if p.partition == 0 else (40, 100)


def test_consumer_lag_uses_committed_offset_or_low_watermark() -> None:
    lag = report_consumer_lag(_LagClient(), topic="t")
    assert lag == {0: 10, 1: 60}
    assert (
        m.REGISTRY.get_sample_value(
            "ekc_kafka_consumer_lag_messages", {"topic": "t", "partition": "1"}
        )
        == 60
    )
    assert report_consumer_lag(_LagClient(), topic="missing") == {}
    _ = uuid  # keep import used for fixtures below
