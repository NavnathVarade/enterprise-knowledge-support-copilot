"""Phase 29: metrics, traces and the audit trail, exercised through the real app paths."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from apps.api.app.main import create_app
from apps.api.app.routes.query import get_query_dependencies
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from packages.audit.service import (
    AuditDetailRejected,
    list_audit_events,
    record_audit,
    verify_chain,
)
from packages.auth.jwt import create_access_token
from packages.config.settings import ObservabilitySettings, get_settings
from packages.database.models import Tenant
from packages.database.session import get_sessionmaker
from packages.ingestion.pipeline import handle_document_ingested
from packages.kafka.producer import EventProducer
from packages.llm.gateway import LLMGateway
from packages.observability.metrics import OUTBOX_BACKLOG, REGISTRY
from packages.observability.tracing import configure_tracing, shutdown_tracing
from packages.orchestration.query_service import QueryDependencies
from packages.storage.object_store import InMemoryObjectStore
from packages.workers.outbox_relay import OutboxRelay
from pydantic import SecretStr
from rag.evaluation.baseline import ExtractiveBaselineProvider
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.test_ingestion_pipeline import (
    EMB,
    FakeKafka,
    _deps,
    _ingested_event,
    _upload,
)

pytestmark = pytest.mark.integration


def _v(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
async def db() -> AsyncSession:
    async with get_sessionmaker(get_settings().database)() as session:
        yield session
        await session.rollback()


@pytest.fixture
def sessionmaker() -> Any:
    return get_sessionmaker(get_settings().database)


async def _new_tenant(db: AsyncSession) -> uuid.UUID:
    t = Tenant(name="Aud", slug=f"aud-{uuid.uuid4().hex[:8]}")
    db.add(t)
    await db.flush()
    tid = t.id
    await db.commit()
    return tid


def _token(tenant: uuid.UUID, roles: frozenset[str] = frozenset()) -> str:
    return create_access_token(
        settings=get_settings().security, user_id=uuid.uuid4(), tenant_id=tenant,
        email="u@example.com", roles=roles, groups=frozenset(),
    )  # fmt: skip


async def _app_client(app: Any):  # type: ignore[no-untyped-def]
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ------------------------------------------------------------------------ HTTP metrics


async def test_http_metrics_use_route_templates_and_bound_unmatched_paths() -> None:
    app = create_app()
    before = _v(
        "ekc_http_requests_total", method="GET", route="/api/v1/health/live", status_class="2xx"
    )
    un_before = _v("ekc_http_requests_total", method="GET", route="unmatched", status_class="4xx")
    async with app.router.lifespan_context(app), await _app_client(app) as c:
        await c.get("/api/v1/health/live")
        for i in range(5):
            await c.get(f"/scanner/probe/{uuid.uuid4()}/{i}")
    assert (
        _v("ekc_http_requests_total", method="GET", route="/api/v1/health/live", status_class="2xx")
        == before + 1
    )
    assert (
        _v("ekc_http_requests_total", method="GET", route="unmatched", status_class="4xx")
        == un_before + 5
    )
    assert _v("ekc_http_requests_in_flight") == 0
    assert (
        _v("ekc_http_request_duration_seconds_count", method="GET", route="/api/v1/health/live")
        >= 1
    )


async def test_metrics_endpoint_is_fail_closed_and_token_protected() -> None:
    app = create_app()
    async with app.router.lifespan_context(app), await _app_client(app) as c:
        assert (await c.get("/metrics")).status_code == 404  # no token configured => no endpoint
        settings = app.state.settings.model_copy(
            update={
                "observability": ObservabilitySettings(metrics_bearer_token=SecretStr("s3cret"))
            }
        )
        app.state.settings = settings
        assert (await c.get("/metrics")).status_code == 401
        assert (
            await c.get("/metrics", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        ok = await c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and "ekc_http_requests_total" in ok.text


# ------------------------------------------------------------------------ query path


async def test_query_emits_metrics_and_spans_without_leaking_the_query(db: AsyncSession) -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(
        ObservabilitySettings(),
        environment="test",
        extra_processors=[SimpleSpanProcessor(exporter)],
    )
    try:
        tenant = await _new_tenant(db)
        secret = "what is the confidential merger codename zebra"
        app = create_app()
        deps = QueryDependencies(
            gateway=LLMGateway(ExtractiveBaselineProvider()),
            embedding_provider=EMB,
            reranker=LexicalOverlapReranker(),
        )
        app.dependency_overrides[get_query_dependencies] = lambda: deps
        before = _v("ekc_query_requests_total", route="RAG_KNOWLEDGE", status="ABSTAINED")
        abst = _v("ekc_answer_abstentions_total", reason="NO_CONTEXT")
        async with app.router.lifespan_context(app), await _app_client(app) as c:
            r = await c.post(
                "/api/v1/query", json={"query": secret},
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )  # fmt: skip
        assert r.status_code == 200
        route = r.json()["route"]["route"]
        assert _v("ekc_query_requests_total", route=route, status="ABSTAINED") >= before + (
            1 if route == "RAG_KNOWLEDGE" else 0
        )
        assert _v("ekc_answer_abstentions_total", reason="NO_CONTEXT") >= abst
        names = {s.name for s in exporter.get_finished_spans()}
        assert "query.run" in names
        dump = " ".join(str(s.attributes) for s in exporter.get_finished_spans())
        assert "zebra" not in dump and "merger" not in dump
        body, _ = __import__("packages.observability.metrics", fromlist=["x"]).render_metrics()
        assert b"zebra" not in body
    finally:
        shutdown_tracing()


# ------------------------------------------------------------------------ ingestion + relay


async def test_one_trace_spans_upload_and_worker_and_outbox_metrics_move(
    db: AsyncSession, sessionmaker: Any
) -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(
        ObservabilitySettings(),
        environment="test",
        extra_processors=[SimpleSpanProcessor(exporter)],
    )
    try:
        store = InMemoryObjectStore()
        from opentelemetry import trace as ot
        from packages.observability.tracing import span

        with span("test.upload_request") as root:
            up = await _upload(db, store)
            # _upload runs the API in-process, but outside this span's context inside the app; so
            # stamp the event with this trace exactly as the route does (current_traceparent).
            event = await _ingested_event(db, up)
        from packages.observability.tracing import current_traceparent

        with span("test.carrier"):
            event = event.model_copy(update={"traceparent": current_traceparent()})
        root_trace = ot.get_current_span().get_span_context().trace_id  # noqa: F841

        done_before = _v("ekc_ingestion_documents_total", outcome="completed")
        await handle_document_ingested(_deps(sessionmaker, store), event)
        assert _v("ekc_ingestion_documents_total", outcome="completed") == done_before + 1
        for stage in ("fetch", "parse_chunk", "embed"):
            assert _v("ekc_ingestion_stage_duration_seconds_count", stage=stage) >= 1

        spans = exporter.get_finished_spans()
        handler = next(s for s in spans if s.name == "ingestion.handle_document_ingested")
        carrier = next(s for s in spans if s.name == "test.carrier")
        assert handler.context.trace_id == carrier.context.trace_id  # worker joined the trace
        assert root.get_span_context().trace_id != 0
        # completion event carries the (continued) trace forward
        from packages.database.models.outbox import OutboxEvent
        from sqlalchemy import select

        db.expire_all()
        rows = (
            (
                await db.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.tenant_id == up.tenant,
                        OutboxEvent.topic == "document.processing.completed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows[0].envelope["traceparent"] is not None

        published_before = _v("ekc_outbox_relay_events_total", result="published")
        relay = OutboxRelay(sessionmaker, EventProducer(FakeKafka("ack")))
        await relay.run_once()
        await relay.sample_backlog()
        assert _v("ekc_outbox_relay_events_total", result="published") > published_before
        assert OUTBOX_BACKLOG._value.get() >= 0  # noqa: SLF001
    finally:
        shutdown_tracing()


# ------------------------------------------------------------------------ audit trail


async def test_audit_records_login_upload_and_query_and_verifies(db: AsyncSession) -> None:
    store = InMemoryObjectStore()
    up = await _upload(db, store)  # login (success) + upload
    app = create_app()
    deps = QueryDependencies(
        gateway=LLMGateway(ExtractiveBaselineProvider()),
        embedding_provider=EMB,
        reranker=LexicalOverlapReranker(),
    )
    app.dependency_overrides[get_query_dependencies] = lambda: deps
    async with app.router.lifespan_context(app), await _app_client(app) as c:
        await c.post(
            "/api/v1/query", json={"query": "refund policy 30 days"},
            headers={"Authorization": f"Bearer {_token(up.tenant)}"},
        )  # fmt: skip
        bad = await c.post(
            "/api/v1/auth/login",
            json={"tenant_slug": "nope", "email": "x@example.com", "password": "pw"},
        )
        assert bad.status_code == 401  # unknown tenant: nothing to attribute it to (not audited)
    async with get_sessionmaker(get_settings().database)() as s:
        events = await list_audit_events(s, up.tenant, limit=50)
        actions = [(e.action, e.outcome) for e in reversed(events)]
        assert actions[:2] == [("auth.login", "success"), ("document.upload", "success")]
        assert ("query.executed", "success") in actions
        upload = next(e for e in events if e.action == "document.upload")
        assert upload.resource_id == str(up.document_id)
        assert upload.details["mime_type"] == "text/markdown"
        q = next(e for e in events if e.action == "query.executed")
        assert (
            set(q.details) == {"route", "status", "query_hash"}
            and len(q.details["query_hash"]) == 12
        )
        assert "refund" not in str([e.details for e in events])  # no query text anywhere
        v = await verify_chain(s, up.tenant)
        assert v.ok and v.checked == len(events)


async def test_failed_logins_for_a_known_tenant_are_audited_without_credentials(
    db: AsyncSession,
) -> None:
    tenant = Tenant(name="Bad", slug=f"bad-{uuid.uuid4().hex[:8]}")
    db.add(tenant)
    await db.flush()
    tid, slug = tenant.id, tenant.slug
    await db.commit()
    app = create_app()
    async with app.router.lifespan_context(app), await _app_client(app) as c:
        r = await c.post(
            "/api/v1/auth/login",
            json={"tenant_slug": slug, "email": "ghost@example.com", "password": "hunter2-secret"},
        )
    assert r.status_code == 401
    async with get_sessionmaker(get_settings().database)() as s:
        (ev,) = await list_audit_events(s, tid)
    assert (ev.action, ev.outcome, ev.details) == (
        "auth.login", "denied", {"reason": "unknown_or_inactive_user"}
    )  # fmt: skip
    assert "ghost" not in str(ev.__dict__) and "hunter2" not in str(ev.__dict__)


async def test_audit_log_is_append_only_and_tampering_is_detected(db: AsyncSession) -> None:
    tid = await _new_tenant(db)
    sm = get_sessionmaker(get_settings().database)
    async with sm() as s:
        for i in range(4):
            await record_audit(s, tenant_id=tid, action="test.event", resource_type="doc",
                               outcome="success", details={"size_bytes": i})  # fmt: skip
        await s.commit()
    # 1) the database refuses edits and deletes
    for stmt in (
        "UPDATE audit_events SET outcome='failure' WHERE tenant_id=:t",
        "DELETE FROM audit_events WHERE tenant_id=:t",
    ):
        async with sm() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                await s.execute(text(stmt), {"t": tid})
    async with sm() as s:
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("TRUNCATE audit_events"))
    # 2) a privileged actor who disables the trigger and edits a row is caught by the chain
    async with sm() as s:
        assert (await verify_chain(s, tid)).ok
        await s.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
        )
        await s.execute(
            text("UPDATE audit_events SET outcome='failure' WHERE tenant_id=:t AND seq=2"),
            {"t": tid},
        )
        v = await verify_chain(s, tid)
        assert not v.ok and v.first_bad_seq == 2 and "hash" in (v.reason or "")
        await s.rollback()  # DDL + edit are transactional: leave the table untouched
    # 3) deleting a middle row (same privilege) shows up as a sequence gap
    async with sm() as s:
        await s.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete")
        )
        await s.execute(text("DELETE FROM audit_events WHERE tenant_id=:t AND seq=3"), {"t": tid})
        v = await verify_chain(s, tid)
        assert not v.ok and v.reason == "gap in sequence" and v.first_bad_seq == 3
        await s.rollback()
    async with sm() as s:
        assert (await verify_chain(s, tid)).ok


async def test_audit_details_are_allow_listed(db: AsyncSession) -> None:
    tid = await _new_tenant(db)
    async with get_sessionmaker(get_settings().database)() as s:
        for bad in (
            {"query": "secret text"},
            {"email": "a@b.c"},
            {"reason": "x" * 500},
            {"route": {"a": 1}},
        ):
            with pytest.raises(AuditDetailRejected):
                await record_audit(s, tenant_id=tid, action="a", resource_type="r",
                                   outcome="success", details=bad)  # fmt: skip
        with pytest.raises(ValueError):
            await record_audit(s, tenant_id=tid, action="a", resource_type="r", outcome="maybe")


async def test_concurrent_writers_produce_a_gapless_linear_chain(db: AsyncSession) -> None:
    tid = await _new_tenant(db)
    sm = get_sessionmaker(get_settings().database)

    async def write(i: int) -> None:
        async with sm() as s:
            await record_audit(s, tenant_id=tid, action="test.concurrent", resource_type="x",
                               outcome="success", details={"size_bytes": i})  # fmt: skip
            await s.commit()

    await asyncio.gather(*(write(i) for i in range(12)))
    async with sm() as s:
        events = await list_audit_events(s, tid, limit=50)
        assert sorted(e.seq for e in events) == list(range(1, 13))
        assert (await verify_chain(s, tid)).ok


async def test_audit_api_is_role_gated_and_tenant_scoped(db: AsyncSession) -> None:
    a, b = await _new_tenant(db), await _new_tenant(db)
    sm = get_sessionmaker(get_settings().database)
    async with sm() as s:
        await record_audit(s, tenant_id=a, action="a.only", resource_type="x", outcome="success")
        await record_audit(s, tenant_id=b, action="b.only", resource_type="x", outcome="success")
        await s.commit()
    app = create_app()
    async with app.router.lifespan_context(app), await _app_client(app) as c:
        member = {"Authorization": f"Bearer {_token(a)}"}
        admin = {"Authorization": f"Bearer {_token(a, frozenset({'admin'}))}"}
        auditor = {"Authorization": f"Bearer {_token(a, frozenset({'security_auditor'}))}"}
        assert (await c.get("/api/v1/audit")).status_code == 401
        assert (await c.get("/api/v1/audit", headers=member)).status_code == 403
        mine = (await c.get("/api/v1/audit", headers=admin)).json()
        assert [e["action"] for e in mine] == ["a.only"]  # tenant B's event is invisible
        assert (await c.get("/api/v1/audit", headers=auditor)).status_code == 200
        v = (await c.get("/api/v1/audit/verify", headers=admin)).json()
        assert v == {"ok": True, "checked": 1, "first_bad_seq": None, "reason": None}
