"""
Query orchestration (Spec §6, §12): route a question, run the route's pipeline, return a
structured result with a trace. This is where Phases 16-24 become one request path:

  route -> [ACL+temporal hybrid retrieval -> rerank -> context (re-auth, quarantine,
  budget) -> generation (injection-hardened, cited, validated)]  |  analytics  |
  access check  |  incident analysis  |  customer investigation

Every pipeline enforces tenant + ACL itself; the router only chooses. Routes that are not
implemented return an explicit NOT_SUPPORTED / NEEDS_INPUT result -- never a guessed
answer. DomainErrors (permission, not-found, LLM failure, policy violation) propagate to
the API's standard error contract.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from rag.comparison.service import compare_document_versions
from rag.context.builder import build_context_package
from rag.embeddings.base import EmbeddingProvider
from rag.generation.answer import Answer, AnswerStatus, generate_answer
from rag.reranking.base import RerankerProvider
from rag.retrieval.reranked_search import reranked_hybrid_search_authorized
from rag.routing.base import QueryRoute, RoutingDecision, TimeMode
from rag.routing.classifier import route_query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.analytics.executor import AnalyticsExecutor, AnalyticsResult
from packages.analytics.llm_planner import plan_intent
from packages.auth.identity import Identity
from packages.database.models.documents import Document, DocumentVersion, DocumentVersionStatus
from packages.database.repositories.authorization import build_document_acl_predicate
from packages.database.repositories.documents import get_current_version
from packages.domain.errors import InvalidAnalyticsQuery
from packages.investigation.knowledge import make_authorized_knowledge_search
from packages.investigation.workflows import analyze_incidents, investigate_customer_issue
from packages.llm.gateway import LLMGateway
from packages.observability.logging import get_logger
from packages.observability.metrics import (
    ABSTENTIONS,
    INJECTION_QUARANTINED,
    QUERY_DURATION,
    QUERY_REQUESTS,
)
from packages.observability.tracing import span

_log = get_logger(component="query_service")
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_RAG_ROUTES = frozenset(
    {
        QueryRoute.RAG_KNOWLEDGE,
        QueryRoute.HYBRID_SEARCH,
        QueryRoute.LEXICAL_SEARCH,
        QueryRoute.DOCUMENT_LOOKUP,
        QueryRoute.UNKNOWN,
    }
)


class QueryStatus(enum.StrEnum):
    ANSWERED = "ANSWERED"
    ABSTAINED = "ABSTAINED"
    ANALYTICS_RESULT = "ANALYTICS_RESULT"
    ACCESS_RESULT = "ACCESS_RESULT"
    INCIDENT_REPORT = "INCIDENT_REPORT"
    INVESTIGATION_REPORT = "INVESTIGATION_REPORT"
    NEEDS_INPUT = "NEEDS_INPUT"
    NOT_SUPPORTED = "NOT_SUPPORTED"


@dataclass(frozen=True)
class QueryDependencies:
    gateway: LLMGateway
    embedding_provider: EmbeddingProvider
    reranker: RerankerProvider
    analytics_executor: AnalyticsExecutor | None = None
    retrieval_limit: int = 10
    candidate_limit: int = 50
    max_context_tokens: int = 4000
    max_chunks_per_document: int = 3
    min_top_rerank_score: float | None = None


@dataclass(frozen=True)
class QueryResult:
    status: QueryStatus
    decision: RoutingDecision
    message: str
    answer: Answer | None = None
    analytics: AnalyticsResult | None = None
    payload: dict[str, Any] | None = None
    trace: dict[str, Any] = field(default_factory=dict)


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    return value


def resolve_as_of(decision: RoutingDecision, now: datetime) -> datetime | None:
    """
    Historical questions retrieve what was in effect at the LAST MOMENT of the requested
    period ("in 2024" -> 2024-12-31T23:59:59). To-date ranges ("last 90 days", "this
    month") and current/none mean "now" (None). A period spanning several versions of one
    document is represented by its final state only -- documented limitation.
    """
    tr = decision.time_range
    if tr.mode is TimeMode.RANGE and tr.end is not None and tr.end < now:
        return tr.end - timedelta(seconds=1)
    return None


async def check_document_access(
    session: AsyncSession, identity: Identity, document_id: uuid.UUID
) -> bool:
    row = await session.execute(
        select(Document.id).where(
            Document.tenant_id == identity.tenant_id,
            Document.id == document_id,
            build_document_acl_predicate(identity.tenant_id, identity),
        )
    )
    return row.scalar_one_or_none() is not None


async def run_query(
    session: AsyncSession,
    *,
    identity: Identity,
    query: str,
    deps: QueryDependencies,
    now: datetime | None = None,
) -> QueryResult:
    started = time.perf_counter()
    now = now or datetime.now(UTC)
    decision = route_query(query, identity, now=now)
    ids = [uuid.UUID(m) for m in _UUID.findall(decision.normalized_query)]

    route_label = decision.route.value
    with span("query.run", route=route_label, tenant_id=str(identity.tenant_id)):
        try:
            result = await _dispatch(session, identity, decision, ids, deps, now)
        except Exception:
            QUERY_REQUESTS.labels(route_label, "error").inc()
            QUERY_DURATION.labels(route_label).observe(time.perf_counter() - started)
            raise
    QUERY_REQUESTS.labels(route_label, result.status.value).inc()
    QUERY_DURATION.labels(route_label).observe(time.perf_counter() - started)
    quarantined = result.trace.get("dropped_injection")
    if isinstance(quarantined, int) and quarantined > 0:
        INJECTION_QUARANTINED.inc(quarantined)
    if result.answer is not None and result.answer.abstention_reason is not None:
        ABSTENTIONS.labels(result.answer.abstention_reason.value).inc()

    trace = dict(result.trace)
    trace["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    result = dataclasses.replace(result, trace=trace)
    _log.info(
        "query_executed",
        tenant_id=str(identity.tenant_id),
        user_id=str(identity.user_id),
        route=decision.route.value,
        status=result.status.value,
        query_hash=hashlib.sha256(decision.normalized_query.encode()).hexdigest()[:12],
        **{k: v for k, v in trace.items() if isinstance(v, int | float | str | bool | None)},
    )
    return result


async def _dispatch(
    session: AsyncSession,
    identity: Identity,
    decision: RoutingDecision,
    ids: list[uuid.UUID],
    deps: QueryDependencies,
    now: datetime,
) -> QueryResult:
    route = decision.route

    if route in _RAG_ROUTES:
        return await _rag(session, identity, decision, deps, now)

    if route is QueryRoute.SQL_ANALYTICS:
        return await _analytics(identity, decision, deps, now)

    if route is QueryRoute.RAG_PLUS_SQL:
        rag = await _rag(session, identity, decision, deps, now)
        sql = await _analytics(identity, decision, deps, now)
        payload = {"analytics_status": sql.status.value, "analytics_message": sql.message}
        return dataclasses.replace(
            rag,
            analytics=sql.analytics,
            payload=payload,
            trace={**rag.trace, "fusion": "side_by_side"},
        )

    if route is QueryRoute.ACCESS_CHECK:
        if not ids:
            return QueryResult(
                QueryStatus.NEEDS_INPUT,
                decision,
                "Please include the document ID you want to check access for.",
            )
        ok = await check_document_access(session, identity, ids[0])
        # Same wording for "no access" and "does not exist": no existence oracle.
        msg = (
            "You have access to this document."
            if ok
            else "You do not have access to this document, or it does not exist."
        )
        return QueryResult(
            QueryStatus.ACCESS_RESULT,
            decision,
            msg,
            payload={"document_id": str(ids[0]), "accessible": ok},
        )

    if route is QueryRoute.INCIDENT_ANALYSIS:
        tr = decision.time_range
        report = await analyze_incidents(
            session,
            identity=identity,
            since=tr.start if tr.mode is TimeMode.RANGE else None,
            until=tr.end if tr.mode is TimeMode.RANGE else None,
            now=now,
        )
        return QueryResult(
            QueryStatus.INCIDENT_REPORT,
            decision,
            f"{len(report.incidents)} incident(s) in window, {len(report.unresolved)} unresolved.",
            payload=to_jsonable(report),
        )

    if route is QueryRoute.SUPPORT_ANALYSIS:
        if not ids:
            return QueryResult(
                QueryStatus.NEEDS_INPUT,
                decision,
                "Please include the customer ID (a UUID) you want investigated.",
            )
        tr = decision.time_range
        investigation = await investigate_customer_issue(
            session,
            identity=identity,
            customer_id=ids[0],
            since=tr.start if tr.mode is TimeMode.RANGE else None,
            until=tr.end if tr.mode is TimeMode.RANGE else None,
            knowledge_search=make_authorized_knowledge_search(
                session, identity, deps.embedding_provider, deps.reranker
            ),
            now=now,
        )
        return QueryResult(
            QueryStatus.INVESTIGATION_REPORT,
            decision,
            f"{len(investigation.tickets)} ticket(s), {len(investigation.candidates)} "
            "correlated lead(s) found (correlation, not confirmed cause).",
            payload=to_jsonable(investigation),
        )

    if route is QueryRoute.DOCUMENT_COMPARISON:
        return await _comparison(session, identity, decision, ids, deps)

    return QueryResult(
        QueryStatus.NOT_SUPPORTED, decision, "This kind of question is not supported."
    )


async def _rag(
    session: AsyncSession,
    identity: Identity,
    decision: RoutingDecision,
    deps: QueryDependencies,
    now: datetime,
) -> QueryResult:
    as_of = resolve_as_of(decision, now)
    candidates = await reranked_hybrid_search_authorized(
        session,
        tenant_id=identity.tenant_id,
        identity=identity,
        query=decision.normalized_query,
        embedding_provider=deps.embedding_provider,
        reranker=deps.reranker,
        limit=deps.retrieval_limit,
        candidate_limit=deps.candidate_limit,
        as_of=as_of,
    )
    package = await build_context_package(
        session,
        tenant_id=identity.tenant_id,
        identity=identity,
        candidates=candidates,
        max_tokens=deps.max_context_tokens,
        max_chunks_per_document=deps.max_chunks_per_document,
        as_of=as_of,
    )
    answer = await generate_answer(
        deps.gateway,
        tenant_id=identity.tenant_id,
        question=decision.normalized_query,
        package=package,
        min_top_rerank_score=deps.min_top_rerank_score,
    )
    s = package.stats
    trace: dict[str, Any] = {
        "retrieved_candidates": len(candidates),
        "context_chunks": len(package.chunks),
        "context_tokens": package.total_tokens,
        "dropped_unauthorized": s.dropped_unauthorized_count,
        "dropped_not_in_effect": s.dropped_not_in_effect_count,
        "dropped_duplicates": s.dropped_duplicate_count,
        "dropped_injection": s.dropped_injection_count,
        "dropped_budget": s.dropped_token_budget_count,
        "as_of": as_of.isoformat() if as_of else None,
        "prompt_name": answer.prompt_name,
        "prompt_version": answer.prompt_version,
    }
    if answer.llm is not None:
        trace.update(
            input_tokens=answer.llm.response.input_tokens,
            output_tokens=answer.llm.response.output_tokens,
            llm_model=answer.llm.response.model,
            llm_cost_usd=answer.llm.cost_usd,
            llm_attempts=answer.llm.attempts,
            llm_fallback_used=answer.llm.fallback_used,
        )
    status = (
        QueryStatus.ANSWERED if answer.status is AnswerStatus.ANSWERED else QueryStatus.ABSTAINED
    )
    return QueryResult(status, decision, answer.text, answer=answer, trace=trace)


async def _analytics(
    identity: Identity,
    decision: RoutingDecision,
    deps: QueryDependencies,
    now: datetime,
) -> QueryResult:
    if deps.analytics_executor is None:
        return QueryResult(
            QueryStatus.NOT_SUPPORTED, decision, "Analytics is not enabled for this deployment."
        )
    try:
        intent = await plan_intent(
            deps.gateway,
            tenant_id=identity.tenant_id,
            question=decision.normalized_query,
            now=now,
        )
        result = await deps.analytics_executor.execute(intent, identity.tenant_id)
    except InvalidAnalyticsQuery:
        return QueryResult(
            QueryStatus.NOT_SUPPORTED, decision, "That analytics question is not supported."
        )
    note = " (truncated)" if result.truncated else ""
    return QueryResult(
        QueryStatus.ANALYTICS_RESULT,
        decision,
        f"{result.row_count} row(s) returned{note}.",
        analytics=result,
        trace={
            "analytics_rows": result.row_count,
            "analytics_duration_ms": round(result.duration_ms, 2),
        },
    )


_PREVIOUS_WORDS = re.compile(
    r"\b(?:previous|prior|older|earlier|last version|before)\b", re.IGNORECASE
)
_NEEDS_DOC = (
    "I could not identify which document to compare. Put its title in quotes "
    '(e.g. "Refund Policy") or include its document ID.'
)


async def _resolve_document(
    session: AsyncSession,
    identity: Identity,
    decision: RoutingDecision,
    ids: list[uuid.UUID],
    deps: QueryDependencies,
) -> tuple[uuid.UUID, str] | None:
    """Returns (document_id, how_resolved). Every path is ACL-enforced."""
    tenant = identity.tenant_id
    acl = build_document_acl_predicate(tenant, identity)
    if ids and await check_document_access(session, identity, ids[0]):
        return ids[0], "document_id"
    for term in decision.entities.quoted_terms:
        row = await session.execute(
            select(Document.id)
            .where(Document.tenant_id == tenant, func.lower(Document.title) == term.lower(), acl)
            .order_by(Document.created_at)
            .limit(1)
        )
        found = row.scalar_one_or_none()
        if found is not None:
            return found, "title"
    top = await reranked_hybrid_search_authorized(
        session,
        tenant_id=tenant,
        identity=identity,
        query=decision.normalized_query,
        embedding_provider=deps.embedding_provider,
        reranker=deps.reranker,
        limit=1,
        candidate_limit=deps.candidate_limit,
    )
    if not top:
        return None
    row = await session.execute(
        select(DocumentVersion.document_id).where(
            DocumentVersion.tenant_id == tenant,
            DocumentVersion.id == top[0].chunk.document_version_id,
        )
    )
    doc_id = row.scalar_one_or_none()
    return (doc_id, "search") if doc_id is not None else None


async def _resolve_versions(
    session: AsyncSession, identity: Identity, decision: RoutingDecision, document_id: uuid.UUID
) -> tuple[int, int] | None:
    refs = decision.entities.version_refs
    if len(refs) >= 2:
        a, b = sorted(refs[:2])
        return a, b
    if refs or not _PREVIOUS_WORDS.search(decision.normalized_query):
        return None  # a single version reference, or no version information at all
    current = await get_current_version(
        session, tenant_id=identity.tenant_id, document_id=document_id
    )
    if current is None:
        return None
    prior = (
        await session.execute(
            select(func.max(DocumentVersion.version)).where(
                DocumentVersion.tenant_id == identity.tenant_id,
                DocumentVersion.document_id == document_id,
                DocumentVersion.version < current.version,
                DocumentVersion.status != DocumentVersionStatus.draft.value,
            )
        )
    ).scalar_one_or_none()
    return (prior, current.version) if prior is not None else None


async def _comparison(
    session: AsyncSession,
    identity: Identity,
    decision: RoutingDecision,
    ids: list[uuid.UUID],
    deps: QueryDependencies,
) -> QueryResult:
    resolved = await _resolve_document(session, identity, decision, ids, deps)
    if resolved is None:
        return QueryResult(QueryStatus.NEEDS_INPUT, decision, _NEEDS_DOC)
    document_id, how = resolved
    versions = await _resolve_versions(session, identity, decision, document_id)
    if versions is None:
        return QueryResult(
            QueryStatus.NEEDS_INPUT,
            decision,
            "Please name the two versions to compare (for example: v2 and v3), "
            "or ask for the current version versus the previous one.",
        )
    result = await compare_document_versions(
        session,
        identity=identity,
        document_id=document_id,
        version_a=versions[0],
        version_b=versions[1],
        gateway=deps.gateway,
        max_context_tokens=deps.max_context_tokens,
    )
    payload: dict[str, Any] = {
        "document_id": str(result.document_id),
        "document_title": result.document_title,
        "version_a": result.version_a,
        "version_b": result.version_b,
        "diff_counts": result.diff_counts,
        "resolved_document_by": how,  # "search" means inferred: verify the title
    }
    trace: dict[str, Any] = {
        "comparison_excerpts": result.excerpts_included,
        "comparison_dropped_budget": result.excerpts_dropped_budget,
        "comparison_quarantined": result.excerpts_quarantined,
    }
    if result.answer is None:
        return QueryResult(
            QueryStatus.ANSWERED,
            decision,
            f'Versions {result.version_a} and {result.version_b} of "{result.document_title}" '
            "have identical content.",
            payload=payload,
            trace=trace,
        )
    answer = result.answer
    if answer.llm is not None:
        trace.update(
            prompt_name=answer.prompt_name,
            prompt_version=answer.prompt_version,
            input_tokens=answer.llm.response.input_tokens,
            output_tokens=answer.llm.response.output_tokens,
            llm_cost_usd=answer.llm.cost_usd,
        )
    status = (
        QueryStatus.ANSWERED if answer.status is AnswerStatus.ANSWERED else QueryStatus.ABSTAINED
    )
    return QueryResult(status, decision, answer.text, answer=answer, payload=payload, trace=trace)
