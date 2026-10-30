"""
Parsing subsystem base types (Section 17).

`ParsedDocument` is the normalized output every mime-type-specific parser produces,
regardless of source format -- downstream chunking (Phase 10) works against this shape,
never against format-specific structures directly. `pages` and `headings` are optional
structure hints (Section 20/22) that a format may or may not be able to supply; plain
text has neither, PDF has pages, DOCX has headings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ParsedHeading:
    text: str
    level: int  # 1 = top-level heading, 2 = subheading, etc.
    character_offset: int  # offset into ParsedDocument.text where this heading starts


@dataclass(frozen=True)
class ParsedPage:
    page_number: int  # 1-indexed, matching how humans refer to "page 3"
    character_start: int
    character_end: int


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    pages: tuple[ParsedPage, ...] = field(default_factory=tuple)
    headings: tuple[ParsedHeading, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)


class Parser(Protocol):
    def parse(self, content: bytes) -> ParsedDocument: ...
