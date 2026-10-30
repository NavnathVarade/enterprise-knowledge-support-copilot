"""
Document version comparison (Spec §16: "what changed between v2 and v3?").

Flow: authorized fetch of BOTH versions (ACL enforced in SQL, drafts excluded, tenant
filtered) -> deterministic chunk diff -> if nothing changed, say so WITHOUT calling a
model -> otherwise build a context of ONLY the differing excerpts (injection-quarantined,
token-budgeted, each labeled with its version) -> versioned comparison prompt -> the same
output policy + citation validation as ordinary answers.

A version number that does not exist, is a draft, or belongs to a document the caller
cannot see is indistinguishable from "not found" (`ResourceNotFound`): comparison is not
an existence oracle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from packages.auth.identity import Identity
from packages.database.models.documents import Document, DocumentChunk
from packages.database.repositories.authorization import build_document_acl_predicate
from packages.database.repositories.chunks import get_chunks_for_version_comparison
from packages.domain.errors import InvalidQuery, ResourceNotFound
from packages.llm.gateway import LLMGateway
from packages.observability.logging import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag.comparison.diff import Block, VersionDiff, diff_versions
from rag.context.base import ContextChunk, ContextPackage, ContextSelectionStats
from rag.generation.answer import Answer, generate_answer
from rag.generation.prompts import COMPARE_PROMPT_V1
from rag.security.injection import InjectionRisk, assess_text

_log = get_logger(component="version_comparison")


@dataclass(frozen=True)
class ComparisonResult:
    document_id: uuid.UUID
    document_title: str
    version_a: int
    version_b: int
    diff_counts: dict[str, int]  # unchanged / added / removed / modified
    answer: Answer | None  # None when the versions are identical
    excerpts_included: int
    excerpts_dropped_budget: int
    excerpts_quarantined: int


def _block(c: DocumentChunk) -> Block:
    return Block(c.id, c.content_hash, c.heading_path, c.sequence)


def _ctx(
    chunk: DocumentChunk, title: str, document_id: uuid.UUID, version: int, classification: str
) -> ContextChunk:
    return ContextChunk(
        chunk_id=chunk.id,
        text=chunk.text,
        token_count=chunk.token_count,
        heading_path=chunk.heading_path,
        page_number=chunk.page_number,
        document_id=document_id,
        document_title=title,
        document_version=version,
        security_classification=classification,
        source_uri=None,
        rerank_score=1.0,
    )


async def compare_document_versions(
    session: AsyncSession,
    *,
    identity: Identity,
    document_id: uuid.UUID,
    version_a: int,
    version_b: int,
    gateway: LLMGateway,
    max_context_tokens: int = 4000,
) -> ComparisonResult:
    if version_a == version_b:
        raise InvalidQuery("Choose two different versions to compare.")
    if version_a > version_b:
        version_a, version_b = version_b, version_a

    tenant = identity.tenant_id
    chunks_a, chunks_b = await get_chunks_for_version_comparison(
        session,
        tenant_id=tenant,
        identity=identity,
        document_id=document_id,
        version_a=version_a,
        version_b=version_b,
    )
    if not chunks_a or not chunks_b:
        raise ResourceNotFound()

    doc_row = (
        await session.execute(
            select(Document.title, Document.security_classification).where(
                Document.tenant_id == tenant,
                Document.id == document_id,
                build_document_acl_predicate(tenant, identity),
            )
        )
    ).one_or_none()
    if doc_row is None:  # defense in depth; the fetch above already enforced the ACL
        raise ResourceNotFound()
    title, classification = doc_row

    diff: VersionDiff = diff_versions([_block(c) for c in chunks_a], [_block(c) for c in chunks_b])
    counts = {
        "unchanged": diff.unchanged_count,
        "added": len(diff.added),
        "removed": len(diff.removed),
        "modified": len(diff.modified),
    }

    def result(
        answer: Answer | None, included: int = 0, dropped: int = 0, quarantined: int = 0
    ) -> ComparisonResult:
        return ComparisonResult(
            document_id=document_id,
            document_title=title,
            version_a=version_a,
            version_b=version_b,
            diff_counts=counts,
            answer=answer,
            excerpts_included=included,
            excerpts_dropped_budget=dropped,
            excerpts_quarantined=quarantined,
        )

    if not diff.has_changes:
        return result(None)

    by_id = {c.id: c for c in [*chunks_a, *chunks_b]}
    ordered: list[ContextChunk] = []
    for pair in diff.modified:  # modifications first: they carry the most information
        ordered.append(
            _ctx(by_id[pair.before.chunk_id], title, document_id, version_a, classification)
        )
        ordered.append(
            _ctx(by_id[pair.after.chunk_id], title, document_id, version_b, classification)
        )
    ordered += [
        _ctx(by_id[b.chunk_id], title, document_id, version_b, classification) for b in diff.added
    ]
    ordered += [
        _ctx(by_id[b.chunk_id], title, document_id, version_a, classification) for b in diff.removed
    ]

    selected: list[ContextChunk] = []
    quarantined = dropped_budget = used = 0
    for c in ordered:
        assessment = assess_text(c.text)
        if assessment.risk >= InjectionRisk.HIGH:
            quarantined += 1
            _log.warning(
                "chunk_quarantined",
                tenant_id=str(tenant),
                chunk_id=str(c.chunk_id),
                document_id=str(document_id),
                signals=list(assessment.signals),
            )
            continue
        if used + c.token_count > max_context_tokens:
            dropped_budget += 1
            continue
        selected.append(c)
        used += c.token_count

    package = ContextPackage(
        chunks=selected,
        total_tokens=used,
        truncated_by_budget=dropped_budget > 0,
        stats=ContextSelectionStats(
            candidates_considered=len(ordered),
            dropped_duplicate_count=0,
            dropped_source_diversity_count=0,
            dropped_token_budget_count=dropped_budget,
            selected_count=len(selected),
            dropped_injection_count=quarantined,
        ),
        distinct_document_count=1,
    )
    question = f"What changed between version {version_a} and version {version_b} of this document?"
    answer = await generate_answer(
        gateway, tenant_id=tenant, question=question, package=package, prompt=COMPARE_PROMPT_V1
    )
    return result(answer, len(selected), dropped_budget, quarantined)
