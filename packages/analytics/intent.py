"""
Structured query intent (Spec §40: LLM -> structured query intent -> validated builder).
`parse_intent` is STRICT: unknown keys anywhere are rejected, so a model cannot smuggle
extra fields (raw SQL, table names, joins) through. Semantic validation (columns exist,
types match, ops allowed) happens in the builder, against the semantic layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.domain.errors import InvalidAnalyticsQuery


@dataclass(frozen=True)
class Metric:
    func: str = "count"
    column: str | None = None


@dataclass(frozen=True)
class Filter:
    column: str
    op: str
    value: Any


@dataclass(frozen=True)
class GroupBy:
    column: str
    bucket: str | None = None  # day|week|month, datetime columns only


@dataclass(frozen=True)
class QueryIntent:
    entity: str
    metric: Metric = field(default_factory=Metric)
    filters: tuple[Filter, ...] = ()
    group_by: tuple[GroupBy, ...] = ()
    time_start: datetime | None = None
    time_end: datetime | None = None
    order: str = "group"  # group | metric_desc | metric_asc
    limit: int | None = None


_TOP = {"entity", "metric", "filters", "group_by", "time_range", "order", "limit"}


def _strict(obj: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise InvalidAnalyticsQuery(f"{where} must be an object")
    extra = set(obj) - allowed
    if extra:
        raise InvalidAnalyticsQuery(f"unknown field(s) in {where}: {sorted(extra)}")
    return obj


def _parse_dt(value: Any, where: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise InvalidAnalyticsQuery(f"{where} is not an ISO-8601 datetime") from exc
    else:
        raise InvalidAnalyticsQuery(f"{where} must be an ISO-8601 datetime string")
    if parsed.tzinfo is None:
        raise InvalidAnalyticsQuery(f"{where} must include a timezone offset")
    return parsed


def parse_intent(data: Any) -> QueryIntent:
    d = _strict(data, _TOP, "intent")
    entity = d.get("entity")
    if not isinstance(entity, str):
        raise InvalidAnalyticsQuery("intent.entity must be a string")

    m = _strict(d.get("metric", {"func": "count"}), {"func", "column"}, "metric")
    metric = Metric(func=str(m.get("func", "count")), column=m.get("column"))
    if metric.column is not None and not isinstance(metric.column, str):
        raise InvalidAnalyticsQuery("metric.column must be a string")

    filters = []
    raw_filters = d.get("filters", [])
    if not isinstance(raw_filters, list):
        raise InvalidAnalyticsQuery("filters must be a list")
    for f in raw_filters:
        fd = _strict(f, {"column", "op", "value"}, "filter")
        if not isinstance(fd.get("column"), str) or not isinstance(fd.get("op"), str):
            raise InvalidAnalyticsQuery("filter needs string column and op")
        filters.append(Filter(fd["column"], fd["op"], fd.get("value")))

    group_by = []
    raw_groups = d.get("group_by", [])
    if not isinstance(raw_groups, list):
        raise InvalidAnalyticsQuery("group_by must be a list")
    for g in raw_groups:
        gd = _strict(g, {"column", "bucket"}, "group_by item")
        if not isinstance(gd.get("column"), str):
            raise InvalidAnalyticsQuery("group_by item needs a string column")
        group_by.append(GroupBy(gd["column"], gd.get("bucket")))

    start = end = None
    if (tr := d.get("time_range")) is not None:
        td = _strict(tr, {"start", "end"}, "time_range")
        if "start" in td:
            start = _parse_dt(td["start"], "time_range.start")
        if "end" in td:
            end = _parse_dt(td["end"], "time_range.end")

    limit = d.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        raise InvalidAnalyticsQuery("limit must be an integer")

    return QueryIntent(
        entity=entity,
        metric=metric,
        filters=tuple(filters),
        group_by=tuple(group_by),
        time_start=start,
        time_end=end,
        order=str(d.get("order", "group")),
        limit=limit,
    )
