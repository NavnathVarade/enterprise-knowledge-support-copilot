"""Phase 31: usage ledger, daily budgets, cost reporting -- real Postgres, real HTTP."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from apps.api.app.main import create_app
from apps.api.app.routes.query import get_query_dependencies
from httpx import ASGITransport, AsyncClient
from packages.audit.service import list_audit_events
from packages.auth.jwt import create_access_token
from packages.config.settings import UsageSettings, get_settings
from packages.database.models import Tenant
from packages.database.models.usage import TenantBudget, UsageEvent
from packages.database.session import get_sessionmaker
from packages.domain.errors import BudgetExceeded
from packages.ingestion.pipeline import handle_document_ingested
from packages.llm.base import LLMMessage, LLMRequest, LLMResponse
from packages.llm.gateway import LLMGateway
from packages.llm.pricing import ModelPricing
from packages.observability.metrics import REGISTRY
from packages.orchestration.query_service import QueryDependencies
from packages.storage.object_store import InMemoryObjectStore
from packages.usage import budget as budget_module
from packages.usage.budget import DbBudgetGuard
from packages.usage.context import usage_context
from packages.usage.recorder import DbUsageRecorder
from packages.usage.summary import usage_summary
from rag.embeddings.base import EmbeddingBatchResult
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.test_ingestion_pipeline import (
    _deps,
    _ingested_event,
    _upload,
)
from tests.integration.test_query_endpoint import _EMB, ScriptedLLM, _doc

pytestmark = pytest.mark.integration

_DB = get_settings().database
_PRICE = {"m-1": ModelPricing(3.0, 15.0)}


def _v(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
async def db() -> AsyncSession:
    async with get_sessionmaker(_DB)() as session:
        yield session
        await session.rollback()


async def _tenant(db: AsyncSession) -> uuid.UUID:
    t = Tenant(name="Usage", slug=f"use-{uuid.uuid4().hex[:8]}")
    db.add(t)
    await db.flush()
    tid = t.id
    await db.commit()
    return tid


class FakeLLM:
    name, model = "fake", "m-1"

    def __init__(self, input_tokens: int = 100, output_tokens: int = 50) -> None:
        self.calls = 0
        self._in, self._out = input_tokens, output_tokens

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse("ok", "fake", "m-1", self._in, self._out, "end_turn", 1.0)


_REQ = LLMRequest(system="s", messages=[LLMMessage("user", "hi")], max_output_tokens=16)


async def _events(db: AsyncSession, tenant: uuid.UUID) -> list[UsageEvent]:
    db.expire_all()
    rows = await db.execute(
        select(UsageEvent).where(UsageEvent.tenant_id == tenant).order_by(UsageEvent.occurred_at)
    )
    return list(rows.scalars().all())


async def _add(
    db: AsyncSession,
    tenant: uuid.UUID,
    *,
    tokens: int = 100,
    cost: str | None = "0.01",
    kind: str = "llm",
    at: datetime | None = None,
    request_id: str | None = None,
    operation: str = "query",
    document_id: uuid.UUID | None = None,
) -> None:
    db.add(
        UsageEvent(
            tenant_id=tenant, kind=kind, operation=operation, provider="p", model="m",
            input_tokens=tokens, output_tokens=0,
            cost_usd=None if cost is None else Decimal(cost),
            request_id=request_id, document_id=document_id,
            **({"occurred_at": at} if at else {}),
        )
    )  # fmt: skip
    await db.commit()


def _guard(**kw: Any) -> DbBudgetGuard:
    return DbBudgetGuard(_DB, UsageSettings(**kw))


# ------------------------------------------------------------------ recording


async def test_gateway_records_every_call_with_context_and_price(db: AsyncSession) -> None:
    t = await _tenant(db)
    uid = uuid.uuid4()
    gw = LLMGateway(FakeLLM(), pricing=_PRICE, usage_recorder=DbUsageRecorder(_DB))
    with usage_context(operation="query", user_id=uid, request_id="req-1"):
        await gw.complete(_REQ, tenant_id=t)
    (e,) = await _events(db, t)
    assert (e.kind, e.operation, e.provider, e.model) == ("llm", "query", "fake", "m-1")
    assert (e.input_tokens, e.output_tokens) == (100, 50)
    assert e.cost_usd == Decimal("0.00105000")  # (100*3 + 50*15) / 1e6
    assert e.user_id == uid and e.request_id == "req-1"


async def test_unpriced_model_records_null_cost_not_zero(db: AsyncSession) -> None:
    t = await _tenant(db)
    gw = LLMGateway(FakeLLM(), usage_recorder=DbUsageRecorder(_DB))
    await gw.complete(_REQ, tenant_id=t)
    (e,) = await _events(db, t)
    assert e.cost_usd is None and e.input_tokens == 100


async def test_recorder_failure_never_breaks_the_caller_and_is_counted(db: AsyncSession) -> None:
    before = _v("ekc_usage_record_failures_total", kind="llm")
    # unknown tenant -> FK violation inside the recorder
    gw = LLMGateway(FakeLLM(), usage_recorder=DbUsageRecorder(_DB))
    result = await gw.complete(_REQ, tenant_id=uuid.uuid4())
    assert result.response.text == "ok"
    assert _v("ekc_usage_record_failures_total", kind="llm") == before + 1


async def test_usage_rows_are_tenant_isolated_by_rls_view(db: AsyncSession) -> None:
    a, b = await _tenant(db), await _tenant(db)
    await _add(db, a)
    await _add(db, b)
    s = await usage_summary(db, a)
    assert sum(d.events for d in s.daily) == 1


# ------------------------------------------------------------------ budgets


async def test_token_budget_blocks_after_spend_and_only_that_tenant(db: AsyncSession) -> None:
    a, b = await _tenant(db), await _tenant(db)
    db.add(TenantBudget(tenant_id=a, daily_token_limit=250))
    await db.commit()
    provider = FakeLLM()  # 150 tokens per call
    gw = LLMGateway(
        provider, budget_guard=_guard(), usage_recorder=DbUsageRecorder(_DB), pricing=_PRICE
    )
    await gw.complete(_REQ, tenant_id=a)  # 150 < 250
    await gw.complete(_REQ, tenant_id=a)  # 150 < 250 at check time; total 300 now
    before = _v("ekc_budget_rejections_total", limit="token")
    outcome_before = _v(
        "ekc_llm_requests_total", provider="fake", model="m-1", outcome="budget_exceeded"
    )
    with pytest.raises(BudgetExceeded):
        await gw.complete(_REQ, tenant_id=a)
    assert provider.calls == 2  # the refused call never reached the provider
    assert _v("ekc_budget_rejections_total", limit="token") == before + 1
    assert (
        _v("ekc_llm_requests_total", provider="fake", model="m-1", outcome="budget_exceeded")
        == outcome_before + 1
    )
    await gw.complete(_REQ, tenant_id=b)  # b has no limit


async def test_cost_budget_counts_known_costs_and_ignores_unpriced(db: AsyncSession) -> None:
    t = await _tenant(db)
    db.add(TenantBudget(tenant_id=t, daily_cost_usd_limit=Decimal("1.00")))
    await db.commit()
    guard = _guard()
    await _add(db, t, cost=None)  # unpriced: contributes nothing
    await guard.check(t)
    await _add(db, t, cost="0.60")
    await guard.check(t)
    await _add(db, t, cost="0.40", kind="embedding")  # embeddings count toward COST
    with pytest.raises(BudgetExceeded):
        await guard.check(t)


async def test_embedding_tokens_do_not_count_toward_the_token_limit(db: AsyncSession) -> None:
    t = await _tenant(db)
    db.add(TenantBudget(tenant_id=t, daily_token_limit=100))
    await db.commit()
    await _add(db, t, tokens=10_000, kind="embedding", operation="ingestion")
    await _guard().check(t)


async def test_yesterdays_spend_does_not_count(db: AsyncSession) -> None:
    t = await _tenant(db)
    db.add(TenantBudget(tenant_id=t, daily_token_limit=100))
    await db.commit()
    await _add(db, t, tokens=10_000, at=datetime.now(UTC) - timedelta(days=1, minutes=1))
    await _guard().check(t)


async def test_env_defaults_apply_without_a_row_and_a_row_overrides_them(
    db: AsyncSession,
) -> None:
    t = await _tenant(db)
    await _add(db, t, tokens=500)
    guard = _guard(default_daily_token_limit=400)
    with pytest.raises(BudgetExceeded):
        await guard.check(t)
    db.add(TenantBudget(tenant_id=t, daily_token_limit=None, daily_cost_usd_limit=None))
    await db.commit()
    await guard.check(t)  # the tenant row (unlimited) wins over the default


async def test_budget_check_failure_follows_fail_open_setting(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = await _tenant(db)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(budget_module, "tenant_scoped_session", boom)
    await _guard(budget_fail_open=True).check(t)
    with pytest.raises(RuntimeError):
        await _guard(budget_fail_open=False).check(t)


# ------------------------------------------------------------------ summary


async def test_summary_reports_unpriced_events_and_per_query_cost(db: AsyncSession) -> None:
    t = await _tenant(db)
    await _add(db, t, cost="0.10", request_id="q1")
    await _add(db, t, cost="0.20", request_id="q1")  # one query, two calls: 0.30
    await _add(db, t, cost="0.50", request_id="q2")  # 0.50
    await _add(db, t, cost="0.10", request_id="q3")
    await _add(db, t, cost=None, request_id="q3")  # q3 has an unpriced call -> excluded
    s = await usage_summary(db, t)
    (llm,) = [d for d in s.daily if d.kind == "llm"]
    assert llm.events == 5 and llm.unpriced_events == 1
    assert llm.known_cost_usd == Decimal("0.90000000")
    qc = s.query_cost
    assert qc.queries_counted == 2 and qc.queries_excluded_unpriced == 1
    assert qc.average_cost_usd == Decimal("0.4")


async def test_summary_with_no_data_has_no_average(db: AsyncSession) -> None:
    s = await usage_summary(db, await _tenant(db))
    assert s.daily == [] and s.query_cost.average_cost_usd is None and s.top_documents == []


async def test_summary_rejects_out_of_range_days(db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await usage_summary(db, await _tenant(db), days=0)


# ------------------------------------------------------------------ ingestion metering


class _MeteredEmbedder(DeterministicLocalEmbeddingProvider):
    def __init__(self, tokens: int | None) -> None:
        super().__init__(dimensions=1024)
        self._tokens = tokens

    async def embed_batch_metered(self, texts: list[str]) -> EmbeddingBatchResult:
        return EmbeddingBatchResult(await self.embed_batch(texts), self._tokens)


async def _ingest(db: AsyncSession, embedder: Any, price: float | None) -> tuple[Any, Any]:
    store = InMemoryObjectStore()
    up = await _upload(db, store, content=b"# Policy\n\nParental leave is 16 weeks.\n")
    sm = get_sessionmaker(_DB)
    deps = _deps(sm, store, embedder)
    deps = type(deps)(
        **{**deps.__dict__, "usage_recorder": DbUsageRecorder(_DB),
           "embedding_usd_per_million_tokens": price}
    )  # fmt: skip
    await handle_document_ingested(deps, await _ingested_event(db, up))
    return up, await _events(db, up.tenant)


async def test_ingestion_records_reported_embedding_tokens_with_document(
    db: AsyncSession,
) -> None:
    up, events = await _ingest(db, _MeteredEmbedder(1234), price=0.5)
    (e,) = events
    assert (e.kind, e.operation, e.document_id) == ("embedding", "ingestion", up.document_id)
    assert e.input_tokens == 1234 and e.cost_usd == Decimal("0.00061700")
    s = await usage_summary(db, up.tenant)
    assert [(d.document_id, d.input_tokens) for d in s.top_documents] == [(up.document_id, 1234)]


async def test_ingestion_never_invents_tokens_when_provider_does_not_report(
    db: AsyncSession,
) -> None:
    _, events = await _ingest(db, DeterministicLocalEmbeddingProvider(dimensions=1024), 0.5)
    (e,) = events
    assert e.input_tokens is None and e.cost_usd is None  # unknown, not zero


# ------------------------------------------------------------------ HTTP


def _token(tenant: uuid.UUID, roles: frozenset[str] = frozenset({"admin"})) -> str:
    return create_access_token(
        settings=get_settings().security, user_id=uuid.uuid4(), tenant_id=tenant,
        email="u@example.com", roles=roles, groups=frozenset(),
    )  # fmt: skip


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_budget_api_roundtrip_is_admin_only_audited_and_tenant_scoped(
    db: AsyncSession,
) -> None:
    a, b = await _tenant(db), await _tenant(db)
    app = create_app()
    async with app.router.lifespan_context(app), _client(app) as c:
        h = {"Authorization": f"Bearer {_token(a)}"}
        hb = {"Authorization": f"Bearer {_token(b)}"}
        nonadmin = {"Authorization": f"Bearer {_token(a, frozenset())}"}
        assert (await c.get("/api/v1/usage/budget", headers=nonadmin)).status_code == 403
        assert (await c.get("/api/v1/usage/summary")).status_code == 401

        first = (await c.get("/api/v1/usage/budget", headers=h)).json()
        assert first["source"] == "default" and first["daily_token_limit"] is None

        r = await c.put(
            "/api/v1/usage/budget",
            json={"daily_token_limit": 5000, "daily_cost_usd_limit": "12.5"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "tenant" and r.json()["daily_token_limit"] == 5000
        assert (await c.get("/api/v1/usage/budget", headers=hb)).json()["source"] == "default"
        for bad in ({"daily_token_limit": 0}, {"daily_cost_usd_limit": "-1"}):
            assert (await c.put("/api/v1/usage/budget", json=bad, headers=h)).status_code == 422
        assert (
            await c.put("/api/v1/usage/budget", json={"daily_token_limit": None}, headers=h)
        ).json()["daily_token_limit"] is None
    events = await list_audit_events(db, a, limit=10)
    assert [e.action for e in events].count("usage.budget_set") == 2


async def test_summary_api_returns_totals_and_the_unknown_cost_note(db: AsyncSession) -> None:
    t = await _tenant(db)
    await _add(db, t, cost=None, request_id="x")
    app = create_app()
    async with app.router.lifespan_context(app), _client(app) as c:
        r = await c.get(
            "/api/v1/usage/summary?days=3", headers={"Authorization": f"Bearer {_token(t)}"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["daily"][0]["unpriced_events"] == 1
        assert "Unknown cost is not zero" in body["note"]
        bad = await c.get(
            "/api/v1/usage/summary?days=999", headers={"Authorization": f"Bearer {_token(t)}"}
        )
        assert bad.status_code == 422


async def test_query_endpoint_meters_usage_then_returns_429_when_budget_is_spent(
    db: AsyncSession,
) -> None:
    t = await _tenant(db)
    await _doc(db, t, "HR Policy", "Parental leave is 16 weeks for all employees.")
    db.add(TenantBudget(tenant_id=t, daily_token_limit=200))  # scripted call = 150 tokens
    await db.commit()
    llm = ScriptedLLM()
    gateway = LLMGateway(
        llm, pricing={"scripted-model": ModelPricing(3.0, 15.0)},
        budget_guard=_guard(), usage_recorder=DbUsageRecorder(_DB),
    )  # fmt: skip
    deps = QueryDependencies(
        gateway=gateway, embedding_provider=_EMB, reranker=LexicalOverlapReranker()
    )
    app = create_app()
    app.dependency_overrides[get_query_dependencies] = lambda: deps
    h = {"Authorization": f"Bearer {_token(t, frozenset())}", "x-request-id": "rid-77"}
    async with app.router.lifespan_context(app), _client(app) as c:
        ok = await c.post(
            "/api/v1/query", json={"query": "What is our parental leave policy?"}, headers=h
        )
        assert ok.status_code == 200 and ok.json()["status"] == "ANSWERED"
        ok2 = await c.post(
            "/api/v1/query", json={"query": "What is our parental leave policy?"}, headers=h
        )
        assert ok2.status_code == 200  # 150 < 200 at check time
        blocked = await c.post(
            "/api/v1/query", json={"query": "What is our parental leave policy?"}, headers=h
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "BUDGET_EXCEEDED"
    events = await _events(db, t)
    assert len(events) == 2 and all(e.request_id == "rid-77" for e in events)
    assert all(e.operation == "query" and e.cost_usd is not None for e in events)
    n_calls = len(llm.requests)
    assert n_calls == 2  # the blocked request never reached the model
    audit = await list_audit_events(db, t, limit=10)
    assert any(
        e.outcome == "denied" and e.details.get("error_code") == "BUDGET_EXCEEDED" for e in audit
    )
    total = (
        await db.execute(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.tenant_id == t)
        )
    ).scalar_one()
    assert total == 2
