"""The harness itself must be trustworthy: it has to DETECT leaks, not merely report zero."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from packages.config.settings import get_settings
from packages.database.session import get_sessionmaker
from packages.llm.gateway import LLMGateway
from packages.orchestration.query_service import QueryDependencies
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.evaluation.answer_eval import evaluate_answers
from rag.evaluation.baseline import ExtractiveBaselineProvider
from rag.evaluation.corpus import load_corpus_into_new_tenant
from rag.evaluation.dataset import (
    load_answer_cases,
    load_corpus,
    load_retrieval_cases,
    load_routing_cases,
)
from rag.evaluation.retrieval_eval import evaluate_retrieval
from rag.evaluation.routing_eval import evaluate_routing
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration
DATA = Path(__file__).resolve().parents[2] / "data" / "eval"
EMB = DeterministicLocalEmbeddingProvider(dimensions=1024)


@pytest.fixture
async def db() -> AsyncSession:
    async with get_sessionmaker(get_settings().database)() as session:
        yield session
        await session.rollback()


async def test_retrieval_eval_has_no_leak_and_finds_labeled_docs(db: AsyncSession) -> None:
    docs = load_corpus(DATA / "retrieval_corpus.jsonl")
    loaded = await load_corpus_into_new_tenant(db, docs, EMB)
    cases = load_retrieval_cases(DATA / "retrieval_cases.jsonl", docs)
    report = await evaluate_retrieval(
        db, loaded, cases, dataset="t", embedding_provider=EMB, reranker=LexicalOverlapReranker()
    )
    assert report.n_cases == len(cases)
    assert report.metrics["restricted_leak_cases"] == 0
    assert report.metrics["cases_with_ranking_labels"] == 12
    assert (report.metrics["recall@5"] or 0) >= 0.9


async def test_retrieval_eval_detects_a_leak_when_acl_is_wrong(db: AsyncSession) -> None:
    # Same corpus, but the "restricted" doc is (mis)configured as public: the harness
    # must flag it, proving a zero in the clean run is meaningful.
    docs = [replace(d, restricted=False) for d in load_corpus(DATA / "retrieval_corpus.jsonl")]
    loaded = await load_corpus_into_new_tenant(db, docs, EMB)
    cases = load_retrieval_cases(DATA / "retrieval_cases.jsonl", docs)
    report = await evaluate_retrieval(
        db, loaded, cases, dataset="t", embedding_provider=EMB, reranker=LexicalOverlapReranker()
    )
    assert report.metrics["restricted_leak_cases"] >= 1
    assert any("LEAK" in f["detail"] for f in report.failures)


async def test_answer_eval_reports_abstention_and_labels_the_baseline(db: AsyncSession) -> None:
    docs = load_corpus(DATA / "retrieval_corpus.jsonl")
    loaded = await load_corpus_into_new_tenant(db, docs, EMB)
    deps = QueryDependencies(
        gateway=LLMGateway(ExtractiveBaselineProvider()),
        embedding_provider=EMB,
        reranker=LexicalOverlapReranker(),
    )
    report = await evaluate_answers(
        db,
        loaded,
        load_answer_cases(DATA / "answer_cases.jsonl", docs),
        deps,
        dataset="t",
        measured_with={"llm": "extractive-baseline (NOT a language model)"},
    )
    assert report.metrics["abstain_rate_on_unanswerable"] == 1.0  # incl. the restricted doc
    assert report.metrics["answerable_cases"] == 8
    assert report.metrics["cited_only_relevant_docs_rate"] == 1.0
    assert "NOT a language model" in report.to_markdown()


def test_routing_eval_runs_and_reports_interval_and_misses() -> None:
    report = evaluate_routing(load_routing_cases(DATA / "routing_cases.jsonl"), dataset="t")
    assert report.metrics["accuracy_ci95_low"] < report.metrics["accuracy"]
    assert report.metrics["accuracy"] < report.metrics["accuracy_ci95_high"]
    assert len(report.failures) == report.n_cases - report.metrics["correct"]
    assert any("Dev-quality" in c for c in report.caveats)
