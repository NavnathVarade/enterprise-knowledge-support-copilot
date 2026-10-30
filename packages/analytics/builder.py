"""
Validated query builder (Spec §40). Turns a QueryIntent into a SQLAlchemy Core SELECT.

Guarantees, all enforced here and covered by tests:
  - SQL is built from semantic-layer metadata only; no caller-supplied string is ever
    interpolated into SQL text -- every value is a bound parameter.
  - Exactly one SELECT statement: no DDL, DML, multi-statement, joins or subqueries.
  - `tenant_id = :tenant` is ALWAYS applied, from the authenticated tenant id passed in,
    never from the intent.
  - Columns, functions, operators and buckets must be in the allow-lists; values are
    type-checked and enum columns restricted to their allowed values.
  - Row limit is capped; the builder asks for limit+1 so the executor can flag truncation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.sql.elements import ColumnElement

from packages.analytics.intent import Filter, GroupBy, QueryIntent
from packages.analytics.semantic_layer import (
    ALLOWED_BUCKETS,
    ALLOWED_FUNCS,
    ALLOWED_OPS,
    DEFAULT_LIMIT,
    ENTITIES,
    MAX_GROUP_BY,
    MAX_IN_VALUES,
    MAX_LIMIT,
    ColumnSpec,
    EntitySpec,
    Kind,
)
from packages.domain.errors import InvalidAnalyticsQuery

METRIC_LABEL = "value"


def _bad(msg: str) -> InvalidAnalyticsQuery:
    return InvalidAnalyticsQuery(msg)


def _coerce(spec: ColumnSpec, value: Any) -> Any:
    if spec.kind is Kind.STR:
        if not isinstance(value, str) or len(value) > 200:
            raise _bad(f"{spec.name} expects a string up to 200 characters")
        if spec.allowed_values is not None and value not in spec.allowed_values:
            raise _bad(f"{spec.name} must be one of {list(spec.allowed_values)}")
        return value
    if spec.kind is Kind.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _bad(f"{spec.name} expects an integer")
        return value
    if spec.kind is Kind.UUID:
        try:
            return uuid.UUID(str(value))
        except ValueError as exc:
            raise _bad(f"{spec.name} expects a UUID") from exc
    if spec.kind is Kind.DATETIME:
        parsed = value if isinstance(value, datetime) else None
        if parsed is None and isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise _bad(f"{spec.name} expects an ISO-8601 datetime") from exc
        if parsed is None or parsed.tzinfo is None:
            raise _bad(f"{spec.name} expects a timezone-aware datetime")
        return parsed
    raise _bad(f"unsupported column kind for {spec.name}")  # pragma: no cover


def _filter_clause(entity: EntitySpec, f: Filter) -> ColumnElement[bool]:
    spec = entity.columns.get(f.column)
    if spec is None or not spec.filterable:
        raise _bad(f"cannot filter on '{f.column}'")
    if f.op not in ALLOWED_OPS:
        raise _bad(f"operator '{f.op}' is not allowed")
    col = entity.table.c[spec.name]
    if f.op == "in":
        if not isinstance(f.value, list) or not 1 <= len(f.value) <= MAX_IN_VALUES:
            raise _bad(f"'in' needs a list of 1-{MAX_IN_VALUES} values")
        return col.in_([_coerce(spec, v) for v in f.value])
    if f.op in ("gt", "gte", "lt", "lte") and spec.kind is Kind.STR:
        raise _bad("range comparison is not allowed on text columns")
    if f.op in ("gt", "gte", "lt", "lte") and spec.kind is Kind.UUID:
        raise _bad("range comparison is not allowed on id columns")
    value = _coerce(spec, f.value)
    clause: ColumnElement[bool]
    if f.op == "eq":
        clause = col == value
    elif f.op == "ne":
        clause = col != value
    elif f.op == "gt":
        clause = col > value
    elif f.op == "gte":
        clause = col >= value
    elif f.op == "lt":
        clause = col < value
    else:  # "lte" -- f.op was validated against ALLOWED_OPS above
        clause = col <= value
    return clause


def _group_expr(entity: EntitySpec, g: GroupBy) -> tuple[ColumnElement[Any], str]:
    spec = entity.columns.get(g.column)
    if spec is None:
        raise _bad(f"unknown column '{g.column}'")
    col = entity.table.c[spec.name]
    if g.bucket is not None:
        if spec.kind is not Kind.DATETIME:
            raise _bad("time bucketing only applies to datetime columns")
        if g.bucket not in ALLOWED_BUCKETS:
            raise _bad(f"bucket must be one of {list(ALLOWED_BUCKETS)}")
        label = f"{spec.name}_{g.bucket}"
        return func.date_trunc(g.bucket, col).label(label), label
    if not spec.groupable:
        raise _bad(f"cannot group by '{g.column}'")
    return col.label(spec.name), spec.name


def _metric_expr(entity: EntitySpec, func_name: str, column: str | None) -> ColumnElement[Any]:
    if func_name not in ALLOWED_FUNCS:
        raise _bad(f"function '{func_name}' is not allowed")
    if func_name == "count":
        if column is not None:
            raise _bad("count takes no column")
        return func.count()
    if column is None:
        raise _bad(f"{func_name} requires a column")
    spec = entity.columns.get(column)
    if spec is None:
        raise _bad(f"unknown column '{column}'")
    col = entity.table.c[spec.name]
    if func_name == "count_distinct":
        if not (spec.groupable or spec.name == "id"):
            raise _bad(f"cannot count distinct '{column}'")
        return func.count(col.distinct())
    if not spec.aggregatable:
        raise _bad(f"cannot aggregate '{column}'")
    if func_name in ("sum", "avg") and spec.kind is not Kind.INT:
        raise _bad(f"{func_name} needs a numeric column")
    agg: ColumnElement[Any]
    if func_name == "sum":
        agg = func.sum(col)
    elif func_name == "avg":
        agg = func.avg(col)
    elif func_name == "min":
        agg = func.min(col)
    else:  # "max" -- func_name was validated against ALLOWED_FUNCS above
        agg = func.max(col)
    return agg


def effective_limit(intent: QueryIntent) -> int:
    limit = DEFAULT_LIMIT if intent.limit is None else intent.limit
    if limit < 1:
        raise _bad("limit must be at least 1")
    return min(limit, MAX_LIMIT)


def build_statement(intent: QueryIntent, tenant_id: uuid.UUID) -> Select[Any]:
    entity = ENTITIES.get(intent.entity)
    if entity is None:
        raise _bad(f"unknown entity '{intent.entity}'")
    if len(intent.group_by) > MAX_GROUP_BY:
        raise _bad(f"at most {MAX_GROUP_BY} group_by columns")
    if intent.order not in ("group", "metric_desc", "metric_asc"):
        raise _bad("order must be group, metric_desc or metric_asc")

    metric = _metric_expr(entity, intent.metric.func, intent.metric.column).label(METRIC_LABEL)
    groups = [_group_expr(entity, g) for g in intent.group_by]

    where: list[ColumnElement[bool]] = [entity.table.c.tenant_id == tenant_id]  # always first
    where.extend(_filter_clause(entity, f) for f in intent.filters)
    time_col = entity.table.c[entity.time_column]
    if intent.time_start is not None:
        if intent.time_start.tzinfo is None:
            raise _bad("time_range.start must be timezone-aware")
        where.append(time_col >= intent.time_start)
    if intent.time_end is not None:
        if intent.time_end.tzinfo is None:
            raise _bad("time_range.end must be timezone-aware")
        where.append(time_col < intent.time_end)

    stmt = select(*(expr for expr, _ in groups), metric).where(and_(*where))
    if groups:
        stmt = stmt.group_by(*(expr for expr, _ in groups))
    if intent.order == "metric_desc":
        stmt = stmt.order_by(metric.desc())
    elif intent.order == "metric_asc":
        stmt = stmt.order_by(metric.asc())
    elif groups:
        stmt = stmt.order_by(*(expr for expr, _ in groups))
    return stmt.limit(effective_limit(intent) + 1)
