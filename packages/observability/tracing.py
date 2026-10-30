"""
OpenTelemetry tracing (Spec §61).

Privacy rule: span attributes are limited to counts, ids, hashes and enums. `safe_attributes`
drops sensitive keys and any string longer than 128 chars, so a query, prompt or chunk can
never be exported to the tracing backend by accident (tests assert this).

The tracer provider is module-local (not the process-global one), so tests can install an
in-memory exporter without fighting OpenTelemetry's set-once global. Context crosses Kafka in
`EventEnvelope.traceparent` (W3C format) so one upload is a single trace through the API,
outbox relay and ingestion worker.

OTLP export is configured from `OTEL_EXPORTER_OTLP_ENDPOINT`; the exporter wiring is
reviewed, NOT verified against a real collector in this environment.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from packages.config.settings import ObservabilitySettings

_SENSITIVE = frozenset(
    {"prompt", "query", "question", "text", "chunk_text", "content", "document_content",
     "password", "token", "api_key", "authorization", "secret", "email"}
)  # fmt: skip
_MAX_STR = 128
_PROPAGATOR = TraceContextTextMapPropagator()
_provider: TracerProvider | None = None


def configure_tracing(
    settings: ObservabilitySettings,
    *,
    environment: str,
    extra_processors: list[SpanProcessor] | None = None,
) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.service_name, "deployment.environment": environment}
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.traces_sample_ratio)),
    )
    if settings.exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.exporter_otlp_endpoint))
        )
    for processor in extra_processors or []:
        provider.add_span_processor(processor)
    global _provider
    _provider = provider
    return provider


def shutdown_tracing() -> None:
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def get_tracer() -> trace.Tracer:
    return (_provider or trace.NoOpTracerProvider()).get_tracer("enterprise-knowledge-copilot")


def safe_attributes(attrs: Mapping[str, Any]) -> dict[str, str | int | float | bool]:
    out: dict[str, str | int | float | bool] = {}
    for key, value in attrs.items():
        if key.lower() in _SENSITIVE or value is None:
            continue
        if (
            isinstance(value, bool | int | float)
            or isinstance(value, str)
            and len(value) <= _MAX_STR
        ):
            out[key] = value
        else:
            out[key] = str(value)[:_MAX_STR] if not isinstance(value, str) else "<redacted:long>"
    return out


@contextmanager
def span(
    name: str, *, parent: otel_context.Context | None = None, **attrs: Any
) -> Iterator[trace.Span]:
    tracer = get_tracer()
    with tracer.start_as_current_span(name, context=parent) as s:
        s.set_attributes(safe_attributes(attrs))
        try:
            yield s
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
            raise


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> otel_context.Context | None:
    if not traceparent:
        return None
    return _PROPAGATOR.extract({"traceparent": traceparent})


def log_correlation_processor(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["otel_trace_id"] = format(ctx.trace_id, "032x")
        event_dict["otel_span_id"] = format(ctx.span_id, "016x")
    return event_dict
