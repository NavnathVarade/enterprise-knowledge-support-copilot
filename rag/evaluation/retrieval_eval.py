"""Retrieval evaluation: the real authorized, reranked hybrid search over a loaded corpus."""

from __future__ import annotations

import time
import uuid

from packages.auth.identity import Identity
from packages.database.models.documents import DocumentVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag.embeddings.base import EmbeddingProvider
from rag.evaluation.corpus import LoadedCorpus
from rag.evaluation.dataset import RetrievalCase
from rag.evaluation.metrics import mean, ndcg_at_k, percentile, recall_at_k, reciprocal_rank
from rag.evaluation.report import EvalReport
from rag.reranking.base import RerankerProvider
from rag.retrieval.reranked_search import reranked_hybrid_search_authorized

KS = (1, 3, 5)


async def evaluate_retrieval(
    session: AsyncSession,
    corpus: LoadedCorpus,
    cases: list[RetrievalCase],
    *,
    dataset: str,
    embedding_provider: EmbeddingProvider,
    reranker: RerankerProvider,
    roles: frozenset[str] = frozenset(),
) -> EvalReport:
    identity = Identity(uuid.uuid4(), corpus.tenant_id, "eval@example.com", roles, frozenset())
    rows = await session.execute(
        select(DocumentVersion.id, DocumentVersion.document_id).where(
            DocumentVersion.tenant_id == corpus.tenant_id
        )
    )
    version_to_doc = {r[0]: r[1] for r in rows.all()}
    per_k: dict[int, list[float | None]] = {k: [] for k in KS}
    mrr: list[float | None] = []
    ndcg: list[float | None] = []
    latencies: list[float] = []
    leaks = 0
    failures = []
    ranked_cases = 0
    for c in cases:
        started = time.perf_counter()
        hits = await reranked_hybrid_search_authorized(
            session,
            tenant_id=corpus.tenant_id,
            identity=identity,
            query=c.query,
            embedding_provider=embedding_provider,
            reranker=reranker,
            limit=10,
        )
        latencies.append((time.perf_counter() - started) * 1000)
        ranked: list[str] = []
        for h in hits:
            key = corpus.key_for(version_to_doc[h.chunk.document_version_id])
            if key is not None and key not in ranked:  # document-level ranking
                ranked.append(key)
        leaked = sorted(set(ranked) & c.forbidden_docs)
        if leaked:
            leaks += 1
            failures.append({"id": c.id, "detail": f"LEAK of restricted docs {leaked}: {c.query}"})
        if c.relevant_docs:
            ranked_cases += 1
            rel = set(c.relevant_docs)
            for k in KS:
                per_k[k].append(recall_at_k(ranked, rel, k))
            mrr.append(reciprocal_rank(ranked, rel))
            ndcg.append(ndcg_at_k(ranked, rel, 5))
            if not set(ranked[:5]) & rel:
                failures.append(
                    {
                        "id": c.id,
                        "detail": f"no relevant doc in top 5 (got {ranked[:5]}): {c.query}",
                    }
                )
    metrics: dict[str, object] = {f"recall@{k}": mean(per_k[k]) for k in KS}
    metrics.update(
        {
            "mrr": mean(mrr),
            "ndcg@5": mean(ndcg),
            "cases_with_ranking_labels": ranked_cases,
            "restricted_leak_cases": leaks,
            "latency_ms_p50": percentile(latencies, 50),
            "latency_ms_p95": percentile(latencies, 95),
        }
    )
    return EvalReport(
        suite="retrieval",
        dataset=dataset,
        n_cases=len(cases),
        metrics=metrics,
        measured_with={
            "embedding": f"{type(embedding_provider).__name__}/{embedding_provider.model_name}",
            "reranker": type(reranker).__name__,
        },
        caveats=[
            "Ranking is at document level (first-hit per document), not chunk level.",
            "Latency is wall-clock on the evaluating machine against a tiny corpus; it is "
            "NOT a load-test result.",
            "With the deterministic embedding provider, vector similarity is NOT semantic; "
            "results mostly reflect full-text search + lexical reranking.",
        ],
        failures=failures,
    )
