"""
Deterministic time-range extraction (Section 13, 16). `now` is injected so results are
reproducible. All datetimes are UTC; ranges are half-open [start, end).

Supported: current/latest/now, yesterday, today, last|past N days|weeks|months,
this|last week|month|quarter|year, "in 2024", "in Q3 2025", "in March 2025".
Anything else yields TimeMode.NONE -- the router never guesses a date.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from rag.routing.base import TimeMode, TimeRange

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}  # fmt: skip
_MONTH_RE = "|".join(_MONTHS)

_LAST_N = re.compile(r"\b(?:last|past)\s+(\d{1,4})\s+(day|week|month)s?\b")
_PERIOD = re.compile(r"\b(this|last)\s+(week|month|quarter|year)\b")
_QUARTER = re.compile(r"\b(?:in\s+)?q([1-4])\s+(\d{4})\b")
_MONTH_YEAR = re.compile(rf"\b(?:in\s+)?({_MONTH_RE})\s+(\d{{4}})\b")
_YEAR = re.compile(r"\bin\s+((?:19|20)\d{2})\b")
_CURRENT = re.compile(r"\b(current(?:ly)?|latest|right now|as of now|today's)\b")
_DAY_WORD = re.compile(r"\b(yesterday|today)\b")


def _start_of_day(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(year: int, month: int) -> datetime:
    return datetime(year + (month - 1) // 12, (month - 1) % 12 + 1, 1, tzinfo=UTC)


def extract_time_range(text: str, *, now: datetime | None = None) -> TimeRange:
    now = now or datetime.now(UTC)
    t = text.lower()

    if m := _QUARTER.search(t):
        q, year = int(m.group(1)), int(m.group(2))
        start = _month_start(year, (q - 1) * 3 + 1)
        return TimeRange(TimeMode.RANGE, start, _month_start(year, (q - 1) * 3 + 4), m.group(0))
    if m := _MONTH_YEAR.search(t):
        month, year = _MONTHS[m.group(1)], int(m.group(2))
        return TimeRange(
            TimeMode.RANGE, _month_start(year, month), _month_start(year, month + 1), m.group(0)
        )
    if m := _YEAR.search(t):
        year = int(m.group(1))
        return TimeRange(
            TimeMode.RANGE,
            datetime(year, 1, 1, tzinfo=UTC),
            datetime(year + 1, 1, 1, tzinfo=UTC),
            m.group(0),
        )
    if m := _LAST_N.search(t):
        n = int(m.group(1))
        days = n * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        return TimeRange(TimeMode.RANGE, now - timedelta(days=days), now, m.group(0))
    if m := _PERIOD.search(t):
        which, unit = m.group(1), m.group(2)
        if unit == "week":
            this_start = _start_of_day(now) - timedelta(days=now.weekday())
            start = this_start if which == "this" else this_start - timedelta(days=7)
            end = now if which == "this" else this_start
        elif unit == "month":
            this_start = _month_start(now.year, now.month)
            start = this_start if which == "this" else _month_start(now.year, now.month - 1)
            end = now if which == "this" else this_start
        elif unit == "quarter":
            q_month = (now.month - 1) // 3 * 3 + 1
            this_start = _month_start(now.year, q_month)
            start = this_start if which == "this" else _month_start(now.year, q_month - 3)
            end = now if which == "this" else this_start
        else:
            this_start = datetime(now.year, 1, 1, tzinfo=UTC)
            start = this_start if which == "this" else datetime(now.year - 1, 1, 1, tzinfo=UTC)
            end = now if which == "this" else this_start
        return TimeRange(TimeMode.RANGE, start, end, m.group(0))
    if m := _DAY_WORD.search(t):
        today = _start_of_day(now)
        if m.group(1) == "yesterday":
            return TimeRange(TimeMode.RANGE, today - timedelta(days=1), today, m.group(0))
        return TimeRange(TimeMode.RANGE, today, now, m.group(0))
    if m := _CURRENT.search(t):
        return TimeRange(TimeMode.CURRENT, matched_text=m.group(0))
    return TimeRange(TimeMode.NONE)
