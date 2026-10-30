from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from packages.analytics.builder import build_statement, effective_limit
from packages.analytics.intent import Filter, GroupBy, Metric, QueryIntent, parse_intent
from packages.analytics.llm_planner import plan_intent
from packages.analytics.semantic_layer import ENTITIES, MAX_LIMIT
from packages.analytics.templates import TEMPLATES, get_template
from packages.domain.errors import InvalidAnalyticsQuery
from packages.llm.base import LLMRequest, LLMResponse
from packages.llm.gateway import LLMGateway
from sqlalchemy.dialects import postgresql

pytestmark = pytest.mark.unit
TENANT = uuid.uuid4()
START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def _sql(intent: QueryIntent) -> tuple[str, dict[str, Any]]:
    c = build_statement(intent, TENANT).compile(dialect=postgresql.dialect())
    return str(c), dict(c.params)


def test_tenant_filter_always_present_and_bound() -> None:
    sql, params = _sql(QueryIntent("tickets"))
    assert "tickets.tenant_id = %(tenant_id_1)s" in sql
    assert params["tenant_id_1"] == TENANT


def test_all_templates_build_cleanly() -> None:
    for name, fn in TEMPLATES.items():
        intent = fn() if name == "open_incidents_by_service" else fn(START, END)
        sql, _ = _sql(intent)
        assert sql.lstrip().upper().startswith("SELECT"), name
        assert "tenant_id" in sql, name


def test_values_are_bound_never_inlined() -> None:
    attack = "x'; DROP TABLE tickets; --"
    with pytest.raises(InvalidAnalyticsQuery):  # enum column rejects it outright
        _sql(QueryIntent("tickets", filters=(Filter("status", "eq", attack),)))
    sql, params = _sql(QueryIntent("tickets", filters=(Filter("channel", "eq", attack),)))
    assert "DROP" not in sql and attack in params.values()


def test_statement_is_single_select_without_joins_or_subqueries() -> None:
    for fn_name, fn in TEMPLATES.items():
        intent = fn() if fn_name == "open_incidents_by_service" else fn(START, END)
        sql, _ = _sql(intent)
        low = sql.lower()
        assert low.count("select") == 1 and " join " not in low and ";" not in sql


@pytest.mark.parametrize(
    "intent",
    [
        QueryIntent("users"),  # unknown entity
        QueryIntent("tickets", filters=(Filter("hashed_password", "eq", "x"),)),
        QueryIntent("tickets", filters=(Filter("status", "like", "op%"),)),
        QueryIntent("tickets", filters=(Filter("status", "gt", "open"),)),
        QueryIntent("tickets", filters=(Filter("id", "gt", str(uuid.uuid4())),)),
        QueryIntent("tickets", filters=(Filter("first_response_seconds", "eq", "5"),)),
        QueryIntent("tickets", filters=(Filter("first_response_seconds", "eq", True),)),
        QueryIntent("tickets", filters=(Filter("status", "in", []),)),
        QueryIntent("tickets", filters=(Filter("status", "in", ["open"] * 51),)),
        QueryIntent("tickets", filters=(Filter("customer_id", "eq", "not-a-uuid"),)),
        QueryIntent("tickets", filters=(Filter("opened_at", "gte", "2026-01-01T00:00:00"),)),
        QueryIntent("tickets", Metric("drop")),
        QueryIntent("tickets", Metric("count", "id")),
        QueryIntent("tickets", Metric("sum")),
        QueryIntent("tickets", Metric("sum", "opened_at")),
        QueryIntent("tickets", Metric("avg", "status")),
        QueryIntent("tickets", Metric("sum", "nope")),
        QueryIntent("tickets", group_by=(GroupBy("id"),)),
        QueryIntent("tickets", group_by=(GroupBy("status", "day"),)),
        QueryIntent("tickets", group_by=(GroupBy("opened_at", "decade"),)),
        QueryIntent("tickets", group_by=(GroupBy("status"),) * 4),
        QueryIntent("tickets", order="random()"),
        QueryIntent("tickets", limit=0),
        QueryIntent("tickets", time_start=datetime(2026, 1, 1)),
    ],
)
def test_invalid_intents_rejected(intent: QueryIntent) -> None:
    with pytest.raises(InvalidAnalyticsQuery):
        build_statement(intent, TENANT)


def test_limit_is_capped_and_plus_one_requested() -> None:
    assert effective_limit(QueryIntent("tickets", limit=10**9)) == MAX_LIMIT
    _, params = _sql(QueryIntent("tickets", limit=10))
    assert 11 in params.values()


def test_time_range_is_half_open_on_entity_time_column() -> None:
    sql, _ = _sql(QueryIntent("tickets", time_start=START, time_end=END))
    assert "tickets.opened_at >=" in sql and "tickets.opened_at <" in sql


def test_time_bucket_uses_date_trunc() -> None:
    sql, _ = _sql(QueryIntent("deployments", group_by=(GroupBy("deployed_at", "day"),)))
    assert "date_trunc" in sql and "deployed_at_day" in sql


def test_semantic_layer_exposes_no_sensitive_columns() -> None:
    exposed = {c for e in ENTITIES.values() for c in e.columns}
    for forbidden in ("email", "hashed_password", "name", "display_name", "body", "notes"):
        assert forbidden not in exposed


def test_every_registered_column_exists_on_its_table() -> None:
    for e in ENTITIES.values():
        for c in e.columns:
            assert c in e.table.c, (e.name, c)
        assert e.time_column in e.table.c


# ----------------------------------------------------------------------- strict parser
def test_parse_intent_round_trip() -> None:
    i = parse_intent(
        {
            "entity": "incidents",
            "metric": {"func": "avg", "column": "affected_customer_count"},
            "filters": [{"column": "severity", "op": "eq", "value": "SEV1"}],
            "group_by": [{"column": "service_name"}],
            "time_range": {"start": "2026-09-01T00:00:00+00:00"},
            "order": "metric_desc",
            "limit": 5,
        }
    )
    assert i.entity == "incidents" and i.limit == 5 and i.time_start == START
    build_statement(i, TENANT)


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"entity": 5},
        {"entity": "tickets", "sql": "select 1"},
        {"entity": "tickets", "metric": {"func": "count", "raw": "1"}},
        {"entity": "tickets", "filters": [{"column": "status", "op": "eq", "value": "x", "or": 1}]},
        {"entity": "tickets", "filters": "status=open"},
        {"entity": "tickets", "time_range": {"start": "yesterday"}},
        {"entity": "tickets", "time_range": {"start": "2026-01-01T00:00:00"}},
        {"entity": "tickets", "limit": "10"},
        {"entity": "tickets", "limit": True},
        {"entity": "tickets", "group_by": [{"column": "status", "extra": 1}]},
    ],
)
def test_parse_intent_is_strict(bad: Any) -> None:
    with pytest.raises(InvalidAnalyticsQuery):
        parse_intent(bad)


def test_get_template_unknown() -> None:
    with pytest.raises(InvalidAnalyticsQuery):
        get_template("drop_everything")


# ---------------------------------------------------------------------------- planner
class _Fake:
    name, model = "fake", "fake"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: LLMRequest | None = None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.seen = request
        return LLMResponse(self.reply, "fake", "fake", 1, 1, "end_turn", 1.0)


async def _plan(reply: str) -> QueryIntent:
    return await plan_intent(
        LLMGateway(_Fake(reply)), tenant_id=TENANT, question="How many tickets?", now=END
    )


async def test_planner_accepts_valid_intent_and_prompt_has_no_secrets() -> None:
    fake = _Fake('{"entity": "tickets", "metric": {"func": "count"}}')
    intent = await plan_intent(LLMGateway(fake), tenant_id=TENANT, question="How many?", now=END)
    assert intent.entity == "tickets"
    assert fake.seen is not None
    assert "password" not in fake.seen.system.lower() and "postgresql://" not in fake.seen.system
    assert "tickets" in fake.seen.system and "never write sql" in fake.seen.system.lower()


@pytest.mark.parametrize(
    "reply",
    [
        '{"entity": "unsupported"}',
        '{"entity": "tickets", "sql": "DROP TABLE tickets"}',
        '{"entity": "users"}',
        '{"entity": "tickets", "filters": [{"column": "hashed_password", "op": "eq", "value": "x"}]}',
        "SELECT * FROM tickets",
    ],
)
async def test_planner_rejects_unsafe_or_unsupported_model_output(reply: str) -> None:
    with pytest.raises(Exception) as ei:  # InvalidAnalyticsQuery or LLMInvalidResponse
        await _plan(reply)
    assert type(ei.value).__name__ in {"InvalidAnalyticsQuery", "LLMInvalidResponse"}
