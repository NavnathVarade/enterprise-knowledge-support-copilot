"""
POST /api/v1/query -- the single entry point for questions (Spec §6).

Identity (and therefore tenant) comes only from the verified token. The request body
carries nothing but the question text. Providers come from `get_query_dependencies`,
built lazily from settings and cached on app.state; tests override the dependency.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from apps.api.app.dependencies.auth import get_current_identity
from apps.api.app.dependencies.db import get_tenant_session
from fastapi import APIRouter, Depends, Request
from packages.audit.service import record_audit
from packages.auth.identity import Identity
from packages.database.session import get_sessionmaker
from packages.domain.errors import (
    BudgetExceeded,
    DomainError,
    PermissionDenied,
    RateLimitExceeded,
    UnauthorizedDocumentAccess,
)
from packages.observability.metrics import AUDIT_WRITE_FAILURES
from packages.orchestration.query_service import (
    QueryDependencies,
    QueryResult,
    run_query,
    to_jsonable,
)
from packages.providers.factory import (
    build_analytics_executor,
    build_embedding_provider,
    build_llm_gateway,
    build_reranker,
)
from packages.redis.client import get_redis_client
from packages.usage.context import usage_context
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/query", tags=["query"])


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class CitationOut(BaseModel):
    source_id: str
    document_id: str
    document_title: str
    document_version: int
    section: str | None
    page: int | None


class RouteOut(BaseModel):
    route: str
    confidence: float
    classifier_version: str
    reason: str
    fallback_used: bool


class QueryResponse(BaseModel):
    status: str
    message: str
    route: RouteOut
    citations: list[CitationOut] = []
    analytics: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    trace: dict[str, Any] = {}


def get_query_dependencies(request: Request) -> QueryDependencies:
    cached: QueryDependencies | None = getattr(request.app.state, "query_dependencies", None)
    if cached is not None:
        return cached
    settings = request.app.state.settings
    redis = get_redis_client(settings.redis)
    deps = QueryDependencies(
        gateway=build_llm_gateway(settings, redis=redis),
        embedding_provider=build_embedding_provider(settings),
        reranker=build_reranker(settings),
        analytics_executor=build_analytics_executor(settings),
    )
    request.app.state.query_dependencies = deps
    return deps


def _present(result: QueryResult) -> QueryResponse:
    d = result.decision
    citations = [
        CitationOut(
            source_id=c.source_id,
            document_id=str(c.document_id),
            document_title=c.document_title,
            document_version=c.document_version,
            section=c.heading_path,
            page=c.page_number,
        )
        for c in (result.answer.citations if result.answer else [])
    ]
    analytics = (
        {
            "columns": result.analytics.columns,
            "rows": to_jsonable(result.analytics.rows),
            "truncated": result.analytics.truncated,
        }
        if result.analytics
        else None
    )
    return QueryResponse(
        status=result.status.value,
        message=result.message,
        route=RouteOut(
            route=d.route.value,
            confidence=d.confidence,
            classifier_version=d.classifier_version,
            reason=d.reason,
            fallback_used=d.fallback_used,
        ),
        citations=citations,
        analytics=analytics,
        payload=result.payload,
        trace=to_jsonable(result.trace),
    )


@router.post("", response_model=QueryResponse)
async def query(
    request: Request,
    body: QueryRequest,
    identity: Identity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_tenant_session),
    deps: QueryDependencies = Depends(get_query_dependencies),
) -> QueryResponse:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    try:
        with usage_context(operation="query", user_id=identity.user_id, request_id=request_id):
            result = await run_query(session, identity=identity, query=body.query, deps=deps)
    except DomainError as exc:
        await _release_connection(session)
        # Denials are audit-relevant; record them on a FRESH session (the request's may be
        # mid-failure), then re-raise unchanged.
        if isinstance(
            exc, PermissionDenied | UnauthorizedDocumentAccess | RateLimitExceeded | BudgetExceeded
        ):
            await _audit_query(request, identity, "denied", request_id, {"error_code": exc.code})
        raise
    await _release_connection(session)
    await _audit_query(
        request,
        identity,
        "success",
        request_id,
        {
            "route": result.decision.route.value,
            "status": result.status.value,
            "query_hash": hashlib.sha256(result.decision.normalized_query.encode()).hexdigest()[
                :12
            ],
        },
    )
    return _present(result)


async def _release_connection(session: AsyncSession) -> None:
    """Return the request session's connection to the pool before the audit write takes another.

    FastAPI keeps the session dependency open until the response is sent, and the audit write uses a
    separate session. Holding both is a nested pool acquisition: with N concurrent requests >= the
    pool size every request holds one connection and waits for a second (Phase 45 measured a
    collapse from ~20 rps to 1.6 rps, p50 10 s). The query path never commits through this session
    (it was always rolled back when the dependency closed), so rolling back changes nothing else.
    """
    await session.rollback()


async def _audit_query(
    request: Request,
    identity: Identity,
    outcome: str,
    request_id: str | None,
    details: dict[str, Any],
) -> None:
    """Best effort (see auth._audit_login): counted on failure, never breaks the answer."""
    try:
        sessionmaker = get_sessionmaker(request.app.state.settings.database)
        async with sessionmaker() as audit_session:
            await record_audit(
                audit_session,
                tenant_id=identity.tenant_id,
                actor_user_id=identity.user_id,
                action="query.executed" if outcome == "success" else "query.denied",
                resource_type="query",
                outcome=outcome,
                request_id=request_id,
                details=details,
            )
            await audit_session.commit()
    except Exception:  # noqa: BLE001
        AUDIT_WRITE_FAILURES.inc()
