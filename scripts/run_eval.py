"""
Evaluation CLI.

    python -m scripts.run_eval routing   [--min accuracy=0.8]
    python -m scripts.run_eval retrieval [--min recall@3=0.8 --min restricted_leak_cases=...]
    python -m scripts.run_eval answer    (offline extractive baseline unless a provider is configured)

Exit code 1 when a --min threshold is violated (CI gate). `--min` is a FLOOR, so leakage is
gated separately by `--max-leaks` (default 0) -- any restricted leak fails the run.
Reports go to stdout as JSON, or markdown with --markdown, and to --out if given.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

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
from rag.evaluation.report import EvalReport, check_thresholds
from rag.evaluation.retrieval_eval import evaluate_retrieval
from rag.evaluation.routing_eval import evaluate_routing
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker

DATA = Path(__file__).resolve().parent.parent / "data" / "eval"


def _parse_min(items: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items:
        name, _, value = item.partition("=")
        if not name or not value:
            raise SystemExit(f"--min expects name=value, got {item!r}")
        out[name] = float(value)
    return out


async def _run(args: argparse.Namespace) -> EvalReport:
    if args.suite == "routing":
        return evaluate_routing(
            load_routing_cases(DATA / "routing_cases.jsonl"), dataset="routing_cases.jsonl"
        )

    corpus_docs = load_corpus(DATA / "retrieval_corpus.jsonl")
    emb = DeterministicLocalEmbeddingProvider(dimensions=1024)
    reranker = LexicalOverlapReranker()
    async with get_sessionmaker(get_settings().database)() as session:
        loaded = await load_corpus_into_new_tenant(session, corpus_docs, emb)
        if args.suite == "retrieval":
            cases = load_retrieval_cases(DATA / "retrieval_cases.jsonl", corpus_docs)
            return await evaluate_retrieval(
                session,
                loaded,
                cases,
                dataset="retrieval_cases.jsonl",
                embedding_provider=emb,
                reranker=reranker,
            )
        deps = QueryDependencies(
            gateway=LLMGateway(ExtractiveBaselineProvider()),
            embedding_provider=emb,
            reranker=reranker,
        )
        return await evaluate_answers(
            session,
            loaded,
            load_answer_cases(DATA / "answer_cases.jsonl", corpus_docs),
            deps,
            dataset="answer_cases.jsonl",
            measured_with={
                "llm": "extractive-baseline (NOT a language model)",
                "embedding": "DeterministicLocalEmbeddingProvider (not semantic)",
                "reranker": "LexicalOverlapReranker",
            },
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("suite", choices=["routing", "retrieval", "answer"])
    ap.add_argument("--min", action="append", default=[], metavar="METRIC=FLOOR")
    ap.add_argument("--max-leaks", type=int, default=0)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    report = asyncio.run(_run(args))
    text = report.to_markdown() if args.markdown else report.to_json()
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")

    violations = check_thresholds(report, _parse_min(args.min))
    leaks = report.metrics.get("restricted_leak_cases")
    if isinstance(leaks, int) and leaks > args.max_leaks:
        violations.append(f"restricted_leak_cases: {leaks} > allowed {args.max_leaks}")
    for v in violations:
        print(f"THRESHOLD VIOLATION: {v}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
