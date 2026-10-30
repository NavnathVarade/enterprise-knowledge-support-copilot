from __future__ import annotations

from scripts.bench_query import QUESTIONS, TOPICS, make_document


def test_documents_are_unique_markdown_with_a_heading_and_content() -> None:
    docs = [make_document(i) for i in range(30)]
    assert len({name for name, _ in docs}) == 30
    assert len({body for _, body in docs}) == 30
    for name, body in docs:
        assert name.endswith(".md") and body.startswith(b"# ")


def test_every_topic_has_a_question() -> None:
    assert len(QUESTIONS) == len(TOPICS)
