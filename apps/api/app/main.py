"""
API service entrypoint.

Responsibilities (Section 78-81, 148):
  - Correlate every request with a request_id/trace_id, propagated to logs and to any
    downstream Kafka events / worker calls.
  - Map DomainError -> the standard {"error": {code, message, request_id}} contract and
    never leak internals.
  - Expose /health/live (process liveness only — never fails due to a downstream outage)
    and /health/ready (checks required dependencies; used by k8s readiness probes to
    pull an instance out of rotation without killing it).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
from collections.abc import Awaitable
from contextlib import asynccontextmanager

import structlog
from apps.api.app.routes import audit as audit_routes
from apps.api.app.routes import auth as auth_routes
from apps.api.app.routes import documents as documents_routes
from apps.api.app.routes import query as query_routes
from apps.api.app.routes import usage as usage_routes
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from packages.config.settings import Environment, get_settings
from packages.domain.errors import DomainError
from packages.observability.logging import configure_logging, get_logger
from packages.observability.metrics import (
    BUILD_INFO,
    HTTP_DURATION,
    HTTP_IN_FLIGHT,
    HTTP_REQUESTS,
    render_metrics,
    status_class,
)
from packages.observability.tracing import configure_tracing, shutdown_tracing
from packages.providers.factory import validate_provider_configuration
from packages.security.http import (
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
    sanitize_request_id,
)
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger: structlog.types.FilteringBoundLogger = get_logger()


class MetricsMiddleware:
    """Pure ASGI (same reasoning as RequestContextMiddleware). The route label is the route
    TEMPLATE (`/api/v1/documents/{id}`), never the raw path: raw paths would make the label
    cardinality unbounded. Requests that match no route share one `unmatched` label so
    scanners cannot inflate the series count."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                holder["status"] = message["status"]
            await send(message)

        HTTP_IN_FLIGHT.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_IN_FLIGHT.dec()
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope.get("method", "GET")
            HTTP_REQUESTS.labels(method, route, status_class(holder.get("status"))).inc()
            HTTP_DURATION.labels(method, route).observe(time.perf_counter() - start)


class RequestContextMiddleware:
    """
    Pure ASGI middleware — deliberately NOT starlette.middleware.base.BaseHTTPMiddleware.

    BaseHTTPMiddleware (the `@app.middleware("http")` decorator) executes the downstream
    application in a separate anyio task and bridges it back via an in-memory stream. In
    that model, an exception raised inside a route handler can fail to reach FastAPI's
    registered `@app.exception_handler(...)` callbacks and instead escapes the ASGI app
    entirely with its raw message intact — a confirmed leak of internal error detail to
    the client. This was caught by
    tests/integration/test_health.py::test_unhandled_exception_never_leaks_internals,
    which failed under the BaseHTTPMiddleware implementation and passes under this one.
    A plain ASGI callable does not fork execution, so exceptions flow through Starlette's
    own ExceptionMiddleware and our registered handlers exactly as if no middleware were
    present.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_headers_in = list(scope.get("headers") or [])
        headers = dict(raw_headers_in)
        # Client-supplied ids are kept only if short and boring (log-injection / oversized
        # values are replaced). The sanitized id is written back into the request headers so
        # every downstream reader (routes, error handlers, usage ledger) sees the same value.
        request_id = sanitize_request_id(headers.get(b"x-request-id", b"").decode("latin-1"))
        trace_id = sanitize_request_id(headers.get(b"x-trace-id", b"").decode("latin-1"))
        scope["headers"] = [
            (k, v) for k, v in raw_headers_in if k not in (b"x-request-id", b"x-trace-id")
        ] + [(b"x-request-id", request_id.encode()), (b"x-trace-id", trace_id.encode())]

        structlog.contextvars.bind_contextvars(request_id=request_id, trace_id=trace_id)
        start = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw_headers = list(message.get("headers", []))
                raw_headers.append((b"x-request-id", request_id.encode()))
                raw_headers.append((b"x-trace-id", trace_id.encode()))
                message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            get_logger().info(
                "request_completed",
                path=scope.get("path", ""),
                method=scope.get("method", ""),
                status_code=status_holder.get("status"),
                duration_ms=round(duration_ms, 2),
            )
            structlog.contextvars.clear_contextvars()


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    configure_logging(
        service_name=settings.observability.service_name,
        environment=settings.environment.value,
        log_level=settings.observability.log_level,
    )
    # Staging/production refuse to start with a test-double embedding provider.
    validate_provider_configuration(settings)
    app.state.settings = settings
    BUILD_INFO.labels(app.version).set(1)
    if settings.environment.is_hardened:
        # Fail the rollout (not the first request) if the database is unreachable.
        from packages.database.session import check_database_connection

        await asyncio.wait_for(
            check_database_connection(settings.database),
            timeout=settings.health.check_timeout_seconds,
        )
    # Tracing is enabled only when an OTLP endpoint is configured; spans are otherwise no-ops.
    owns_tracing = bool(settings.observability.exporter_otlp_endpoint)
    if owns_tracing:
        provider = configure_tracing(settings.observability, environment=settings.environment.value)
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, excluded_urls="health,metrics"
        )
    dispatcher_stop = asyncio.Event()
    dispatcher_task: asyncio.Task[None] | None = None
    if settings.local_event_dispatch_enabled:
        # Development substitute for Kafka: handles outbox events in this process.
        from packages.database.session import get_sessionmaker
        from packages.workers.wiring import build_local_dispatcher

        dispatcher = build_local_dispatcher(settings, get_sessionmaker(settings.database))
        dispatcher_task = asyncio.create_task(
            dispatcher.run_forever(dispatcher_stop), name="local-event-dispatcher"
        )
    # Which integrations are active, never where they point or how to log in to them.
    get_logger().info(
        "service_startup",
        environment=settings.environment.value,
        kafka_enabled=settings.kafka_enabled,
        local_event_dispatch=dispatcher_task is not None,
        object_store="local-disk" if settings.uses_local_object_store else "s3",
        login_throttle_fail_open=settings.hardening.login_throttle_fail_open,
        llm_configured=settings.llm.api_key is not None,
        embeddings="provider" if settings.embedding.api_key is not None else "deterministic-dev",
    )
    try:
        yield
    finally:
        dispatcher_stop.set()
        if dispatcher_task is not None:
            try:
                await asyncio.wait_for(dispatcher_task, timeout=30)
            except TimeoutError:
                dispatcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatcher_task
    if owns_tracing:  # never tear down a provider this app did not create
        shutdown_tracing()
    get_logger().info("service_shutdown")


def _docs_enabled() -> bool:
    """Interactive docs and the OpenAPI schema are served only in development/test unless
    HARDENING_DOCS_ENABLED says otherwise: they describe every endpoint to an attacker."""
    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001 -- misconfiguration is reported at startup, not here
        return False
    flag = settings.hardening.docs_enabled
    if flag is not None:
        return flag
    return settings.environment in (Environment.development, Environment.test)


def create_app() -> FastAPI:
    docs = _docs_enabled()
    app = FastAPI(
        title="Enterprise Knowledge & Support Copilot API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    # add_middleware order: the LAST one added is the OUTERMOST. Outer -> inner:
    # security headers, request context, metrics, body limit, application.
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(auth_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(usage_routes.router)
    app.include_router(documents_routes.router)
    app.include_router(query_routes.router)

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        request_id = request.headers.get("x-request-id", "unknown")
        get_logger().warning(
            "domain_error",
            error_code=exc.code,
            path=request.url.path,
            **{k: v for k, v in exc.context.items() if k not in {"password", "token"}},
        )
        retry_after = getattr(exc, "retry_after_seconds", None)
        return JSONResponse(
            status_code=exc.http_status,
            headers={"Retry-After": str(int(retry_after))} if retry_after else None,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.safe_message,
                    "request_id": request_id,
                }
            },
        )

    def _retryable(request: Request, status: int, code: str, message: str) -> JSONResponse:
        request_id = request.headers.get("x-request-id", "unknown")
        return JSONResponse(
            status_code=status,
            headers={"Retry-After": "1"},
            content={"error": {"code": code, "message": message, "request_id": request_id}},
        )

    # Dependency failures are overload/outage, not bugs: tell the client to retry (503/504 with
    # Retry-After) instead of an opaque 500. Only the exception CLASS is logged, never its text
    # (connection errors can contain hostnames and credentials-bearing URLs).
    @app.exception_handler(PoolTimeoutError)
    async def handle_pool_exhausted(request: Request, exc: PoolTimeoutError) -> JSONResponse:
        get_logger().warning("db_pool_exhausted", path=request.url.path)
        return _retryable(
            request, 503, "SERVICE_BUSY", "The service is busy. Please retry shortly."
        )

    @app.exception_handler(ConnectionError)
    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def handle_dependency_down(request: Request, exc: Exception) -> JSONResponse:
        get_logger().warning(
            "dependency_unavailable", path=request.url.path, error_class=type(exc).__name__
        )
        return _retryable(
            request,
            503,
            "DEPENDENCY_UNAVAILABLE",
            "A required service is temporarily unavailable. Please retry shortly.",
        )

    @app.exception_handler(TimeoutError)
    async def handle_timeout(request: Request, exc: TimeoutError) -> JSONResponse:
        get_logger().warning("dependency_timeout", path=request.url.path)
        return _retryable(
            request, 504, "DEPENDENCY_TIMEOUT", "A required service timed out. Please retry."
        )

    @app.exception_handler(DBAPIError)
    async def handle_db_error(request: Request, exc: DBAPIError) -> JSONResponse:
        # 57014 = query_canceled, i.e. statement_timeout fired. Everything else (integrity,
        # data, programming errors) is a real bug and stays a 500.
        if getattr(exc.orig, "sqlstate", None) == "57014":
            get_logger().warning("db_statement_timeout", path=request.url.path)
            return _retryable(
                request, 504, "DATABASE_TIMEOUT", "The database took too long. Please retry."
            )
        return await handle_unexpected_error(request, exc)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = request.headers.get("x-request-id", "unknown")
        get_logger().error("unhandled_exception", path=request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred.",
                    "request_id": request_id,
                }
            },
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """Fail-closed: with no OTEL_METRICS_BEARER_TOKEN configured the endpoint does not
        exist (404). With one, the exact token is required (constant-time compare)."""
        token = request.app.state.settings.observability.metrics_bearer_token
        if token is None:
            return Response(status_code=404)
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), token.get_secret_value().encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    @app.get("/api/v1/health/live", tags=["health"])
    async def health_live() -> dict[str, str]:
        # Liveness must never depend on downstream dependencies (Section 81 rule):
        # a temporarily unreachable Postgres/Redis/Kafka should not cause Kubernetes
        # to kill and restart a perfectly healthy process.
        return {"status": "alive"}

    @app.get("/api/v1/health/ready", tags=["health"])
    async def health_ready(request: Request) -> JSONResponse:
        settings = request.app.state.settings
        bound = settings.health.check_timeout_seconds
        redis_required = settings.health.redis_required
        if redis_required is None:
            # Redis backs fail-open protections; it gates readiness only when an operator made
            # the login throttle fail closed (then logins are impossible without it).
            redis_required = not settings.hardening.login_throttle_fail_open
        checks: dict[str, str] = {}
        overall_ok = True
        degraded = False

        async def probe(name: str, check: Awaitable[None], required: bool) -> None:
            nonlocal overall_ok, degraded
            try:
                await asyncio.wait_for(check, timeout=bound)
                checks[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
                label = "timeout" if isinstance(exc, TimeoutError) else exc.__class__.__name__
                if required:
                    checks[name] = f"unavailable: {label}"
                    overall_ok = False
                else:
                    checks[name] = f"degraded: {label}"
                    degraded = True

        from packages.database.session import check_database_connection
        from packages.redis.client import check_redis_connection

        probes = [
            probe("database", check_database_connection(settings.database), True),
            probe("redis", check_redis_connection(settings.redis), redis_required),
        ]
        if settings.kafka_enabled:
            from packages.kafka.health import check_kafka_connection

            # The API only writes to the Postgres outbox; the relay/consumer need the broker.
            # A broker outage is therefore "degraded" unless HEALTH_KAFKA_REQUIRED says otherwise.
            probes.append(
                probe(
                    "kafka",
                    check_kafka_connection(settings.kafka, timeout_seconds=bound),
                    settings.health.kafka_required,
                )
            )
        else:
            checks["kafka"] = "disabled"
        # Concurrent, each bounded: a hung dependency costs `bound` seconds, not their sum.
        await asyncio.gather(*probes)

        return JSONResponse(
            status_code=200 if overall_ok else 503,
            content={
                "status": ("ready" if not degraded else "degraded") if overall_ok else "not_ready",
                "checks": checks,
            },
        )

    return app


app = create_app()
