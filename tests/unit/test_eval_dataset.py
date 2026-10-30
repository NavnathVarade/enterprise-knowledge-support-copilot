from __future__ import annotations

from pathlib import Path

import pytest
from rag.evaluation.dataset import (
    DatasetError,
    load_answer_cases,
    load_corpus,
    load_retrieval_cases,
    load_routing_cases,
)
from rag.evaluation.report import EvalReport, check_thresholds

pytestmark = pytest.mark.unit
DATA = Path(__file__).resolve().parents[2] / "data" / "eval"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_shipped_datasets_are_valid_and_consistent() -> None:
    corpus = load_corpus(DATA / "retrieval_corpus.jsonl")
    assert load_routing_cases(DATA / "routing_cases.jsonl")
    assert load_retrieval_cases(DATA / "retrieval_cases.jsonl", corpus)
    answers = load_answer_cases(DATA / "answer_cases.jsonl", corpus)
    assert any(not a.answerable for a in answers) and any(a.answerable for a in answers)


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"id": "a", "query": "q", "expected_route": "NOPE"}',
        '{"id": "a", "query": "", "expected_route": "RAG_KNOWLEDGE"}',
        "[1, 2]",
    ],
)
def test_routing_loader_rejects_bad_rows_with_location(tmp_path: Path, line: str) -> None:
    with pytest.raises(DatasetError, match=r"r\.jsonl(:1)?"):
        load_routing_cases(_write(tmp_path, "r.jsonl", line))


def test_duplicate_ids_and_empty_files_rejected(tmp_path: Path) -> None:
    row = '{"id": "a", "query": "q", "expected_route": "RAG_KNOWLEDGE"}\n'
    with pytest.raises(DatasetError, match="duplicate"):
        load_routing_cases(_write(tmp_path, "d.jsonl", row * 2))
    with pytest.raises(DatasetError, match="no cases"):
        load_routing_cases(_write(tmp_path, "e.jsonl", "# only a comment\n"))


def test_retrieval_labels_must_reference_the_corpus(tmp_path: Path) -> None:
    corpus = load_corpus(
        _write(tmp_path, "c.jsonl", '{"key": "k1", "title": "T", "text": "body"}\n')
    )
    bad = _write(tmp_path, "q.jsonl", '{"id": "q", "query": "x", "relevant_docs": ["zzz"]}\n')
    with pytest.raises(DatasetError, match="unknown document keys"):
        load_retrieval_cases(bad, corpus)
    both = _write(
        tmp_path,
        "b.jsonl",
        '{"id": "q", "query": "x", "relevant_docs": ["k1"], "forbidden_docs": ["k1"]}\n',
    )
    with pytest.raises(DatasetError, match="relevant and forbidden"):
        load_retrieval_cases(both, corpus)
    empty = _write(tmp_path, "n.jsonl", '{"id": "q", "query": "x", "relevant_docs": []}\n')
    with pytest.raises(DatasetError, match="must not be empty"):
        load_retrieval_cases(empty, corpus)


def test_unanswerable_case_cannot_carry_facts(tmp_path: Path) -> None:
    corpus = load_corpus(_write(tmp_path, "c.jsonl", '{"key": "k", "title": "T", "text": "b"}\n'))
    p = _write(
        tmp_path,
        "a.jsonl",
        '{"id": "a", "query": "q", "answerable": false, "expected_facts": ["x"]}\n',
    )
    with pytest.raises(DatasetError, match="unanswerable"):
        load_answer_cases(p, corpus)


def test_threshold_check_treats_missing_metric_as_violation() -> None:
    r = EvalReport("routing", "d", 1, {"accuracy": 0.9, "other": None}, {})
    assert check_thresholds(r, {"accuracy": 0.8}) == []
    assert check_thresholds(r, {"accuracy": 0.95})
    assert check_thresholds(r, {"other": 0.1})
    assert check_thresholds(r, {"absent": 0.1})
    assert "n=1" in r.to_markdown() and r.to_json()
