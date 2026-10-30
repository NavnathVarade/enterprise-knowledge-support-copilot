"""
Context builder (Section 28-29): the last gate before the LLM.

Takes reranked candidates and returns a `ContextPackage`. It does NOT trust that the
candidates were authorized upstream: one join query re-applies the tenant filter, the
ACL predicate and the temporal predicate (defense in depth, Section 11) and resolves
citation metadata from the database rather than from the candidate objects. Any
candidate failing the re-check is dropped and counted, never passed on.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from packages.auth.identity import Identity
from packages.database.models.documents import Document, DocumentChunk, DocumentVersion
from packages.database.repositories.authorization import build_document_acl_predicate
from packages.database.repositories.temporal import build_version_temporal_predicate
from packages.observability.logging import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag.context.base import ContextChunk, ContextPackage, ContextSelectionStats
from rag.context.selection import CandidateInfo, select_for_context
from rag.retrieval.reranked_search import RetrievalCandidate
from rag.security.injection import InjectionRisk, assess_text

_log = get_logger(component="context_builder")


async def build_context_package(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity: Identity,
    candidates: list[RetrievalCandidate],
    max_tokens: int = 4000,
    max_chunks_per_document: int = 3,
    as_of: datetime | None = None,
    quarantine_at: InjectionRisk = InjectionRisk.HIGH,
) -> ContextPackage:
    if identity.tenant_id != tenant_id:
        raise ValueError("identity.tenant_id does not match tenant_id")

    if not candidates:
        return _empty_package()

    score_by_id = {c.chunk.id: c.rerank_score for c in candidates}
    order = [c.chunk.id for c in candidates]

    in_effect = build_version_temporal_predicate(as_of)
    stmt = (
        select(DocumentChunk, DocumentVersion, Document, in_effect.label("in_effect"))
        .join(
            DocumentVersion,
            (DocumentVersion.id == DocumentChunk.document_version_id)
            & (DocumentVersion.tenant_id == DocumentChunk.tenant_id),
        )
        .join(
            Document,
            (Document.id == DocumentVersion.document_id)
            & (Document.tenant_id == DocumentVersion.tenant_id),
        )
        .where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.id.in_(order),
            build_document_acl_predicate(tenant_id, identity),
        )
    )
    rows = (await session.execute(stmt)).all()

    by_id: dict[uuid.UUID, tuple[DocumentChunk, DocumentVersion, Document]] = {}
    not_in_effect = 0
    for chunk, version, document, effective in rows:
        if not effective:
            not_in_effect += 1
            continue
        by_id[chunk.id] = (chunk, version, document)

    unauthorized = len(set(order)) - len(rows)

    # Prompt-injection quarantine: a chunk whose text trips the detector at/above
    # `quarantine_at` never reaches the model (heuristic layer; see Phase 24 doc). Dropped
    # chunks do not pin versions or consume budget. Only ids/signals are logged, never text.
    signals_by_id: dict[uuid.UUID, tuple[str, ...]] = {}
    quarantined = 0
    for cid in list(by_id):
        assessment = assess_text(by_id[cid][0].text)
        if assessment.risk >= quarantine_at:
            quarantined += 1
            _log.warning(
                "chunk_quarantined",
                tenant_id=str(tenant_id),
                chunk_id=str(cid),
                document_id=str(by_id[cid][2].id),
                signals=list(assessment.signals),
            )
            del by_id[cid]
        elif assessment.risk > InjectionRisk.NONE:
            signals_by_id[cid] = assessment.signals

    # Preserve the caller's (rerank) order for rows that survived the re-check.
    ordered_ids = [cid for cid in dict.fromkeys(order) if cid in by_id]
    infos = [
        CandidateInfo(
            chunk_id=cid,
            document_id=by_id[cid][2].id,
            content_hash=by_id[cid][0].content_hash,
            token_count=by_id[cid][0].token_count,
            rerank_score=score_by_id[cid],
            document_version=by_id[cid][1].version,
        )
        for cid in ordered_ids
    ]
    result = select_for_context(
        infos, max_tokens=max_tokens, max_chunks_per_document=max_chunks_per_document
    )

    chunks = []
    for cid in result.selected_ids:
        chunk, version, document = by_id[cid]
        chunks.append(
            ContextChunk(
                chunk_id=chunk.id,
                text=chunk.text,
                token_count=chunk.token_count,
                heading_path=chunk.heading_path,
                page_number=chunk.page_number,
                document_id=document.id,
                document_title=document.title,
                document_version=version.version,
                security_classification=document.security_classification,
                source_uri=document.source_uri,
                rerank_score=score_by_id[cid],
                injection_signals=signals_by_id.get(cid, ()),
            )
        )

    stats = ContextSelectionStats(
        candidates_considered=len(set(order)),
        dropped_duplicate_count=result.dropped_duplicate_count,
        dropped_source_diversity_count=result.dropped_source_diversity_count,
        dropped_token_budget_count=result.dropped_token_budget_count,
        selected_count=len(chunks),
        dropped_version_inconsistent_count=result.dropped_version_inconsistent_count,
        dropped_unauthorized_count=unauthorized,
        dropped_not_in_effect_count=not_in_effect,
        dropped_injection_count=quarantined,
    )
    return ContextPackage(
        chunks=chunks,
        total_tokens=result.total_tokens,
        truncated_by_budget=result.truncated_by_budget,
        stats=stats,
        distinct_document_count=len({c.document_id for c in chunks}),
    )


def _empty_package() -> ContextPackage:
    return ContextPackage(
        chunks=[],
        total_tokens=0,
        truncated_by_budget=False,
        stats=ContextSelectionStats(0, 0, 0, 0, 0),
        distinct_document_count=0,
    )
