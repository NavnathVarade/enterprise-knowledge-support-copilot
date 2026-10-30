"""ACL-enforced knowledge lookup for investigations: a thin adapter over Phase 17's
`reranked_hybrid_search_authorized`, so unreadable documents cannot become references."""

from __future__ import annotations

import uuid

from rag.embeddings.base import EmbeddingProvider
from rag.reranking.base import RerankerProvider
from rag.retrieval.reranked_search import reranked_hybrid_search_authorized
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.auth.identity import Identity
from packages.database.models.documents import Document, DocumentVersion
from packages.investigation.workflows import KnowledgeRef, KnowledgeSearch


def make_authorized_knowledge_search(
    session: AsyncSession,
    identity: Identity,
    embedding_provider: EmbeddingProvider,
    reranker: RerankerProvider,
    *,
    per_query_limit: int = 3,
) -> KnowledgeSearch:
    async def search(query: str, kind: str) -> list[KnowledgeRef]:
        results = await reranked_hybrid_search_authorized(
            session,
            tenant_id=identity.tenant_id,
            identity=identity,
            query=query,
            embedding_provider=embedding_provider,
            reranker=reranker,
            limit=per_query_limit,
        )
        refs = []
        for r in results:
            title = await _title_for(session, r.chunk.document_version_id)
            refs.append(KnowledgeRef(r.chunk.id, title[0], title[1], r.rerank_score, kind))
        return refs

    return search


async def _title_for(session: AsyncSession, version_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    row = (
        await session.execute(
            select(Document.id, Document.title)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(DocumentVersion.id == version_id)
        )
    ).one()
    return row[0], row[1]
