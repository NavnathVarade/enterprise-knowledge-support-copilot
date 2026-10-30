"""Who/what a metered call is for, carried in a ContextVar so the LLM gateway and the
embedding stage can attribute spend without every function signature growing arguments."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageContext:
    operation: str = "other"  # query | ingestion | other
    user_id: uuid.UUID | None = None
    request_id: str | None = None
    document_id: uuid.UUID | None = None


_current: ContextVar[UsageContext | None] = ContextVar("ekc_usage_context", default=None)


def current_context() -> UsageContext:
    return _current.get() or UsageContext()


@contextmanager
def usage_context(
    *,
    operation: str,
    user_id: uuid.UUID | None = None,
    request_id: str | None = None,
    document_id: uuid.UUID | None = None,
) -> Iterator[None]:
    token = _current.set(UsageContext(operation, user_id, request_id, document_id))
    try:
        yield
    finally:
        _current.reset(token)
