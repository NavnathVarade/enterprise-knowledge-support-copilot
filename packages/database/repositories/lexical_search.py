"""
Lexical (full-text) search over chunks (Section 25).

HONEST NAMING NOTE: the brief asks for "BM25 or equivalent". Postgres full-text search
is an *equivalent role* (a lexical, term-matching retriever complementing dense vectors)
but it is NOT BM25: `ts_rank_cd` scores by term frequency and cover density within a
document, with no corpus-wide inverse-document-frequency component, so a rare,
discriminating term is not automatically weighted above a common one the way BM25 would
weight it. Accepted for this reference implementation (index-backed, filterable by
tenant in the same SQL statement as the vector search, no extra service) and recorded
as a known limitation. Whether it needs replacing is a question for Phase 28's retrieval
evaluation, not something to assume either way.

QUERY SEMANTICS -- OR, not AND. The first implementation used `websearch_to_tsquery`,
which ANDs terms together. That failed a real test: "refund policy" returned nothing for
a chunk containing "refund" but not "policy". For RAG that is a recall bug -- a
natural-language question routinely contains words the answering chunk lacks -- so the
lexical leg would silently return zero candidates. Lexical retrieval should favor recall
and let ranking order the results (chunks matching more query terms score higher under
`ts_rank_cd`), with the reranker (Phase 15) removing the extra noise OR semantics admits.

The OR query is built from `plainto_tsquery`, which -- like `websearch_to_tsquery` -- never
raises on arbitrary user input (operators and punctuation are ignored, not parsed). Its
already-normalized output (`'refund' & 'polici'`) is rewritten to `|` and re-parsed with the
`simple` configuration so lexemes are not stemmed a second time. Trade-off accepted: unlike
websearch syntax, users cannot use "quoted phrases" or -exclusions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.auth.identity import Identity
from packages.database.models.documents import Document, DocumentChunk, DocumentVersion
from packages.database.repositories.authorization import build_document_acl_predicate
from packages.database.repositories.temporal import build_version_temporal_predicate


def _or_tsquery(query: str) -> ColumnElement[str]:
    normalized = func.plainto_tsquery("english", query)
    or_text = func.regexp_replace(cast(normalized, Text), " & ", " | ", "g")
    return func.to_tsquery("simple", or_text)


async def search_chunks_lexical(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    limit: int = 20,
) -> list[tuple[DocumentChunk, float]]:
    """Returns (chunk, score) pairs, best match first. The tenant filter is part of the
    same statement as the text match -- never applied afterwards."""
    if not query.strip():
        return []

    ts_query = _or_tsquery(query)
    score = func.ts_rank_cd(DocumentChunk.text_search, ts_query)

    result = await session.execute(
        select(DocumentChunk, score.label("score"))
        .where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.text_search.op("@@")(ts_query),
        )
        .order_by(score.desc(), DocumentChunk.id)
        .limit(limit)
    )
    return [(row[0], float(row[1])) for row in result.all()]


async def search_chunks_lexical_authorized(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    identity: Identity,
    query: str,
    limit: int = 20,
    as_of: datetime | None = None,
) -> list[tuple[DocumentChunk, float]]:
    """ACL-aware, temporally-correct counterpart to search_chunks_lexical -- see
    embeddings.py::find_nearest_chunks_authorized for the shared reasoning."""
    if not query.strip():
        return []

    ts_query = _or_tsquery(query)
    score = func.ts_rank_cd(DocumentChunk.text_search, ts_query)

    result = await session.execute(
        select(DocumentChunk, score.label("score"))
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.text_search.op("@@")(ts_query),
            build_document_acl_predicate(tenant_id, identity),
            build_version_temporal_predicate(as_of),
        )
        .order_by(score.desc(), DocumentChunk.id)
        .limit(limit)
    )
    return [(row[0], float(row[1])) for row in result.all()]
