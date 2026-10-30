from __future__ import annotations

import pytest
from rag.chunking.chunker import chunk_document
from rag.chunking.tokenizer import count_tokens
from rag.parsing.base import ParsedDocument, ParsedHeading, ParsedPage


@pytest.mark.unit
def test_short_document_is_a_single_chunk() -> None:
    doc = ParsedDocument(text="A short document with just one paragraph.")
    chunks = chunk_document(doc, target_tokens=400)
    assert len(chunks) == 1
    assert chunks[0].text == doc.text
    assert chunks[0].sequence == 0


@pytest.mark.unit
def test_empty_document_produces_no_chunks() -> None:
    assert chunk_document(ParsedDocument(text="")) == []
    assert chunk_document(ParsedDocument(text="   \n\n   ")) == []


@pytest.mark.unit
def test_long_document_is_split_into_multiple_chunks_within_budget() -> None:
    paragraph = "This is a sentence that takes up a bit of space. " * 20
    text = "\n\n".join(paragraph for _ in range(10))
    doc = ParsedDocument(text=text)
    chunks = chunk_document(doc, target_tokens=200, overlap_tokens=0)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= 220


@pytest.mark.unit
def test_chunk_sequence_numbers_are_ordered() -> None:
    text = "\n\n".join(f"Paragraph number {i}. " * 30 for i in range(8))
    chunks = chunk_document(ParsedDocument(text=text), target_tokens=100, overlap_tokens=0)
    assert [c.sequence for c in chunks] == list(range(len(chunks)))


@pytest.mark.unit
def test_chunking_is_deterministic() -> None:
    text = "\n\n".join(f"Paragraph {i} has some content in it." for i in range(15))
    doc = ParsedDocument(text=text)
    a = chunk_document(doc, target_tokens=50, overlap_tokens=10)
    b = chunk_document(doc, target_tokens=50, overlap_tokens=10)
    assert a == b


@pytest.mark.unit
def test_heading_path_reflects_nesting() -> None:
    text = (
        "# Refund Policy\n\n"
        "Intro paragraph.\n\n"
        "## Eligibility\n\n"
        + "Eligibility details here that are reasonably long to form their own chunk. " * 3
        + "\n\n## Exceptions\n\n"
        + "Exception details that are also long enough to form their own chunk. " * 3
    )
    headings = (
        ParsedHeading(text="Refund Policy", level=1, character_offset=text.index("# Refund")),
        ParsedHeading(text="Eligibility", level=2, character_offset=text.index("## Eligibility")),
        ParsedHeading(text="Exceptions", level=2, character_offset=text.index("## Exceptions")),
    )
    doc = ParsedDocument(text=text, headings=headings)
    chunks = chunk_document(doc, target_tokens=40, overlap_tokens=0)

    heading_paths = {c.heading_path for c in chunks}
    assert "Refund Policy > Eligibility" in heading_paths
    assert "Refund Policy > Exceptions" in heading_paths


@pytest.mark.unit
def test_chunks_do_not_straddle_heading_boundaries_when_substantial() -> None:
    section_a = "Section A content. " * 30
    section_b = "Section B content. " * 30
    text = f"## Section A\n\n{section_a}\n\n## Section B\n\n{section_b}"
    headings = (
        ParsedHeading(text="Section A", level=2, character_offset=text.index("## Section A")),
        ParsedHeading(text="Section B", level=2, character_offset=text.index("## Section B")),
    )
    doc = ParsedDocument(text=text, headings=headings)
    chunks = chunk_document(doc, target_tokens=1000, overlap_tokens=0)

    section_a_chunks = [c for c in chunks if c.heading_path == "Section A"]
    section_b_chunks = [c for c in chunks if c.heading_path == "Section B"]
    assert section_a_chunks
    assert section_b_chunks
    assert "Section B content" not in section_a_chunks[0].text


@pytest.mark.unit
def test_overlap_carries_trailing_paragraph_into_next_chunk() -> None:
    paragraphs = [f"Distinct paragraph {i} with unique content xyz{i}. " * 5 for i in range(6)]
    text = "\n\n".join(paragraphs)
    doc = ParsedDocument(text=text)
    chunks = chunk_document(doc, target_tokens=60, overlap_tokens=30)

    assert len(chunks) >= 2
    found_overlap = False
    for i in range(len(chunks) - 1):
        tail_words = chunks[i].text.split()[-5:]
        head_words = chunks[i + 1].text.split()[:20]
        if any(w in head_words for w in tail_words):
            found_overlap = True
            break
    assert found_overlap


@pytest.mark.unit
def test_zero_overlap_produces_no_shared_content() -> None:
    paragraphs = [f"Paragraph {i} unique content marker_{i}. " * 10 for i in range(6)]
    text = "\n\n".join(paragraphs)
    doc = ParsedDocument(text=text)
    chunks = chunk_document(doc, target_tokens=50, overlap_tokens=0)

    for i in range(len(chunks) - 1):
        assert chunks[i].character_end <= chunks[i + 1].character_start


@pytest.mark.unit
def test_oversized_single_paragraph_is_split_by_sentence() -> None:
    huge_paragraph = "This is one sentence. " * 200
    doc = ParsedDocument(text=huge_paragraph)
    chunks = chunk_document(doc, target_tokens=50, overlap_tokens=0)

    assert len(chunks) > 1
    combined_length = sum(len(c.text) for c in chunks)
    assert combined_length > len(huge_paragraph) * 0.9


@pytest.mark.unit
def test_page_number_assigned_from_parsed_pages() -> None:
    page1_text = "Content on page one. " * 10
    page2_text = "Content on page two. " * 10
    text = page1_text + "\n\n" + page2_text
    pages = (
        ParsedPage(page_number=1, character_start=0, character_end=len(page1_text) + 2),
        ParsedPage(page_number=2, character_start=len(page1_text) + 2, character_end=len(text)),
    )
    doc = ParsedDocument(text=text, pages=pages)
    chunks = chunk_document(doc, target_tokens=30, overlap_tokens=0)

    page_numbers = {c.page_number for c in chunks}
    assert 1 in page_numbers
    assert 2 in page_numbers


@pytest.mark.unit
def test_document_with_no_pages_has_none_page_number() -> None:
    doc = ParsedDocument(text="Just some plain text with no page info.")
    chunks = chunk_document(doc)
    assert all(c.page_number is None for c in chunks)


@pytest.mark.unit
def test_chunk_token_count_matches_tokenizer() -> None:
    doc = ParsedDocument(text="A reasonably short piece of text for token counting.")
    chunks = chunk_document(doc)
    assert chunks[0].token_count == count_tokens(chunks[0].text)


@pytest.mark.unit
def test_character_ranges_are_valid_and_ordered() -> None:
    text = "\n\n".join(f"Paragraph {i} content here for testing purposes." for i in range(10))
    chunks = chunk_document(ParsedDocument(text=text), target_tokens=30, overlap_tokens=0)
    for chunk in chunks:
        assert 0 <= chunk.character_start < chunk.character_end <= len(text)
