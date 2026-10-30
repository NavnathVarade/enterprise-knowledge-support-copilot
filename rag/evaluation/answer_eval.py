"""
End-to-end answer evaluation through the production `run_query` path.

Per case it records: answered vs abstained, expected-fact presence, citation validity
(validated by the pipeline itself: an invalid citation becomes an abstention/error, so a
"cited" answer here has passed validation), whether a cited document is a labeled relevant
one, and lexical support of the answer text by its cited chunks.
"""

from __future__ import annotations

import time
import uuid

from packages.auth.identity import Identity
from packages.database.models.documents import DocumentVersion
from packages.orchestration.query_service import QueryDependencies, QueryStatus, run_query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag.evaluation.corpus import LoadedCorpus
from rag.evaluation.dataset import AnswerCase
from rag.evaluation.metrics import abstention_scores, lexical_support, mean, percentile
from rag.evaluation.report import EvalReport


async def evaluate_answers(
    session: AsyncSession,
    corpus: LoadedCorpus,
    cases: list[AnswerCase],
    deps: QueryDependencies,
    *,
    dataset: str,
    measured_with: dict[str, str],
) -> EvalReport:
    identity = Identity(
        uuid.uuid4(), corpus.tenant_id, "eval@example.com", frozenset(), frozenset()
    )
    chunk_text: dict[uuid.UUID, str] = {}
    pairs: list[tuple[bool, bool]] = []
    fact_hits: list[float | None] = []
    cited_relevant: list[float | None] = []
    support: list[float | None] = []
    latencies: list[float] = []
    failures = []
    for c in cases:
        started = time.perf_counter()
        result = await run_query(session, identity=identity, query=c.query, deps=deps)
        latencies.append((time.perf_counter() - started) * 1000)
        answered = result.status is QueryStatus.ANSWERED and result.answer is not None
        pairs.append((c.answerable, answered))
        if c.answerable != answered:
            failures.append(
                {
                    "id": c.id,
                    "detail": f"answerable={c.answerable} but status={result.status.value}: {c.query}",
                }
            )
        if not (c.answerable and answered and result.answer is not None):
            continue
        text = result.answer.text
        fact_hits.append(1.0 if all(f.lower() in text.lower() for f in c.expected_facts) else 0.0)
        cited_docs = {cit.document_id for cit in result.answer.citations}
        rel_ids = {corpus.document_ids[k] for k in c.relevant_docs}
        cited_relevant.append(1.0 if cited_docs and cited_docs <= rel_ids else 0.0)
        if fact_hits[-1] == 0.0:
            failures.append({"id": c.id, "detail": f"missing expected facts in: {text[:120]}"})
        for cit in result.answer.citations:
            if cit.chunk_id not in chunk_text:
                chunk_text[cit.chunk_id] = await _chunk_text(session, corpus, cit.chunk_id)
            support.append(lexical_support(text, chunk_text[cit.chunk_id]))
    s = abstention_scores(pairs)
    return EvalReport(
        suite="answer",
        dataset=dataset,
        n_cases=len(cases),
        metrics={
            "answerable_cases": s.answerable,
            "unanswerable_cases": s.unanswerable,
            "answer_rate_on_answerable": s.answer_rate_on_answerable,
            "abstain_rate_on_unanswerable": s.abstain_rate_on_unanswerable,
            "expected_fact_rate_when_answered": mean(fact_hits),
            "cited_only_relevant_docs_rate": mean(cited_relevant),
            "mean_lexical_support": mean(support),
            "latency_ms_p50": percentile(latencies, 50),
            "latency_ms_p95": percentile(latencies, 95),
        },
        measured_with=measured_with,
        caveats=[
            "Fact check is substring presence, not semantic correctness.",
            "lexical_support is a word-overlap heuristic; it cannot detect reversed meaning. "
            "Real faithfulness needs a human or NLI/LLM judge (NOT YET MEASURED).",
            "If measured_with names the extractive baseline, this exercises the pipeline and "
            "harness only; it says nothing about a real LLM.",
        ],
        failures=failures,
    )


async def _chunk_text(session: AsyncSession, corpus: LoadedCorpus, chunk_id: uuid.UUID) -> str:
    from packages.database.models.documents import DocumentChunk

    row = (
        await session.execute(
            select(DocumentChunk.text)
            .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
            .where(DocumentChunk.tenant_id == corpus.tenant_id, DocumentChunk.id == chunk_id)
        )
    ).scalar_one_or_none()
    return row or ""
