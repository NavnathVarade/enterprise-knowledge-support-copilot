"""
Approved query templates (Spec §39): vetted, named intents for the questions people ask
most. Templates only construct a QueryIntent; they still go through the same builder
validation and executor protections as anything else.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from packages.analytics.intent import Filter, GroupBy, Metric, QueryIntent
from packages.domain.errors import InvalidAnalyticsQuery


def tickets_created(start: datetime, end: datetime) -> QueryIntent:
    return QueryIntent("tickets", Metric("count"), time_start=start, time_end=end)


def tickets_by_status(start: datetime, end: datetime) -> QueryIntent:
    return QueryIntent(
        "tickets", Metric("count"), group_by=(GroupBy("status"),), time_start=start, time_end=end
    )


def open_incidents_by_service() -> QueryIntent:
    return QueryIntent(
        "incidents",
        Metric("count"),
        filters=(Filter("status", "in", ["open", "investigating", "mitigated"]),),
        group_by=(GroupBy("service_name"),),
        order="metric_desc",
    )


def incidents_by_severity(start: datetime, end: datetime) -> QueryIntent:
    return QueryIntent(
        "incidents",
        Metric("count"),
        group_by=(GroupBy("severity"),),
        time_start=start,
        time_end=end,
    )


def incidents_by_root_cause(start: datetime, end: datetime) -> QueryIntent:
    return QueryIntent(
        "incidents",
        Metric("count"),
        group_by=(GroupBy("root_cause_category"),),
        time_start=start,
        time_end=end,
        order="metric_desc",
    )


def avg_first_response_by_service(start: datetime, end: datetime) -> QueryIntent:
    return QueryIntent(
        "tickets",
        Metric("avg", "first_response_seconds"),
        group_by=(GroupBy("service_name"),),
        time_start=start,
        time_end=end,
    )


def deployments_per_day(start: datetime, end: datetime) -> QueryIntent:
    return QueryIntent(
        "deployments",
        Metric("count"),
        group_by=(GroupBy("deployed_at", "day"),),
        time_start=start,
        time_end=end,
    )


TEMPLATES: dict[str, Callable[..., QueryIntent]] = {
    "tickets_created": tickets_created,
    "tickets_by_status": tickets_by_status,
    "open_incidents_by_service": open_incidents_by_service,
    "incidents_by_severity": incidents_by_severity,
    "incidents_by_root_cause": incidents_by_root_cause,
    "avg_first_response_by_service": avg_first_response_by_service,
    "deployments_per_day": deployments_per_day,
}


def get_template(name: str) -> Callable[..., QueryIntent]:
    try:
        return TEMPLATES[name]
    except KeyError:
        raise InvalidAnalyticsQuery(f"unknown template '{name}'") from None
