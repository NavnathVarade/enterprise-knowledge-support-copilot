"""
Chunking engine (Section 20-21).

Deliberately NOT "chunk_size=500, overlap=50 and nothing else" (the exact anti-pattern
Section 20 calls out). Three things shape a chunk boundary, in priority order:

  1. Heading boundaries: when the current buffer already has meaningful content and the
     next paragraph starts under a DIFFERENT heading path, the chunk is flushed first --
     a chunk straddling "Eligibility" and "Exceptions" is worse than two clean chunks.
  2. Token budget: paragraphs are packed greedily up to `target_tokens`.
  3. A hard fallback for a single paragraph that exceeds the budget on its own (a huge
     block of unbroken text): split by sentence boundaries rather than mid-word.

Overlap is implemented at PARAGRAPH granularity (the last paragraph(s) of a chunk are
repeated as the start of the next one, up to `overlap_tokens`) rather than sub-paragraph
token slicing -- this keeps every chunk's text a clean concatenation of whole paragraphs,
which matters for readability and for heading_path staying meaningful for the whole chunk.
"""

from __future__ import annotations

import re

from rag.chunking.base import Chunk
from rag.chunking.tokenizer import count_tokens
from rag.parsing.base import ParsedDocument, ParsedHeading, ParsedPage

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


class _Paragraph:
    __slots__ = ("text", "start", "end", "heading_path")

    def __init__(self, text: str, start: int, end: int, heading_path: str | None) -> None:
        self.text = text
        self.start = start
        self.end = end
        self.heading_path = heading_path


def _split_paragraphs(text: str) -> list[tuple[str, int, int]]:
    """Returns (paragraph_text, start_offset, end_offset), skipping blank paragraphs."""
    paragraphs: list[tuple[str, int, int]] = []
    cursor = 0
    for part in _PARAGRAPH_SPLIT.split(text):
        start = text.index(part, cursor) if part else cursor
        end = start + len(part)
        if part.strip():
            paragraphs.append((part, start, end))
        cursor = end
    return paragraphs


def _heading_path_at(offset: int, headings: tuple[ParsedHeading, ...]) -> str | None:
    """
    Builds a breadcrumb like "Title > Section > Subsection" from every heading at or
    before `offset`, maintaining proper nesting: a new heading pops any stack entries
    at its level or deeper before being pushed.
    """
    stack: list[ParsedHeading] = []
    for heading in sorted(headings, key=lambda h: h.character_offset):
        if heading.character_offset > offset:
            break
        while stack and stack[-1].level >= heading.level:
            stack.pop()
        stack.append(heading)
    if not stack:
        return None
    return " > ".join(h.text for h in stack)


def _page_number_at(offset: int, pages: tuple[ParsedPage, ...]) -> int | None:
    for page in pages:
        if page.character_start <= offset < page.character_end:
            return page.page_number
    return None


def _split_oversized_paragraph(paragraph: _Paragraph, *, target_tokens: int) -> list[_Paragraph]:
    """Fallback for a single paragraph that exceeds the token budget alone: split by
    sentence boundary rather than mid-word."""
    sentences = _SENTENCE_SPLIT.split(paragraph.text)
    if len(sentences) <= 1:
        return [paragraph]

    result: list[_Paragraph] = []
    cursor = paragraph.start
    buffer = ""
    buffer_start = cursor
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if buffer and count_tokens(candidate) > target_tokens:
            result.append(
                _Paragraph(buffer, buffer_start, buffer_start + len(buffer), paragraph.heading_path)
            )
            buffer = sentence
            buffer_start = cursor
        else:
            buffer = candidate
        cursor += len(sentence) + 1
    if buffer:
        result.append(
            _Paragraph(buffer, buffer_start, buffer_start + len(buffer), paragraph.heading_path)
        )
    return result


def chunk_document(
    parsed: ParsedDocument, *, target_tokens: int = 400, overlap_tokens: int = 50
) -> list[Chunk]:
    paragraphs = [
        _Paragraph(text, start, end, _heading_path_at(start, parsed.headings))
        for text, start, end in _split_paragraphs(parsed.text)
    ]

    expanded: list[_Paragraph] = []
    for p in paragraphs:
        if count_tokens(p.text) > target_tokens:
            expanded.extend(_split_oversized_paragraph(p, target_tokens=target_tokens))
        else:
            expanded.append(p)
    paragraphs = expanded

    if not paragraphs:
        return []

    chunks: list[Chunk] = []
    buffer: list[_Paragraph] = []
    buffer_tokens = 0

    def make_chunk_from_buffer(source: list[_Paragraph]) -> Chunk:
        text = "\n\n".join(p.text for p in source)
        return Chunk(
            sequence=len(chunks),
            text=text,
            token_count=count_tokens(text),
            heading_path=source[0].heading_path,
            page_number=_page_number_at(source[0].start, parsed.pages),
            character_start=source[0].start,
            character_end=source[-1].end,
        )

    # A small ABSOLUTE threshold, not a fraction of target_tokens: this distinguishes
    # "a trivial one-line intro sitting right after a heading" (should not force a
    # flush on its own) from "genuine section content" (should always respect the
    # heading boundary), regardless of how large target_tokens happens to be. Using a
    # target_tokens-relative threshold was tried first and is a real bug this comment
    # documents: with a large budget, two entirely separate, substantial sections
    # could be merged into a single chunk because neither individually cleared a
    # threshold scaled to the (large) budget -- caught by
    # test_chunks_do_not_straddle_heading_boundaries_when_substantial.
    _MIN_SUBSTANTIAL_TOKENS = 20

    for paragraph in paragraphs:
        p_tokens = count_tokens(paragraph.text)
        heading_changed = bool(buffer) and buffer[0].heading_path != paragraph.heading_path
        buffer_is_substantial = buffer_tokens >= _MIN_SUBSTANTIAL_TOKENS
        would_exceed_budget = buffer_tokens + p_tokens > target_tokens

        if buffer and (would_exceed_budget or (heading_changed and buffer_is_substantial)):
            flushed_paragraphs = buffer
            chunks.append(make_chunk_from_buffer(flushed_paragraphs))

            # Paragraph-granularity overlap: carry the tail of the just-flushed
            # chunk's paragraphs forward, up to overlap_tokens.
            carried: list[_Paragraph] = []
            carried_tokens = 0
            for prev_p in reversed(flushed_paragraphs):
                t = count_tokens(prev_p.text)
                if carried_tokens + t > overlap_tokens:
                    break
                carried.insert(0, prev_p)
                carried_tokens += t
            buffer = carried
            buffer_tokens = carried_tokens

        buffer.append(paragraph)
        buffer_tokens += p_tokens

    if buffer:
        chunks.append(make_chunk_from_buffer(buffer))

    return chunks
