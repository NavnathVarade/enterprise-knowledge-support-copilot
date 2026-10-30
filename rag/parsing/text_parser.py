"""
Plain text and Markdown parser.

Handles what Section 17/113 actually requires beyond "just decode UTF-8": a BOM prefix
must be stripped, CRLF/CR line endings must be normalized to LF (mixed line endings
otherwise corrupt downstream character-offset math for chunking), and a file that isn't
valid UTF-8 or Latin-1 is correctly treated as corrupted input, not silently mangled.

Markdown heading extraction (`# `, `## `, etc.) feeds Section 20's heading-aware
chunking -- deliberately simple line-prefix matching, not a full CommonMark parser,
since that's unjustified complexity for what chunking actually needs (heading text,
level, position).
"""

from __future__ import annotations

import re

from packages.domain.errors import ParsingFailed

from rag.parsing.base import ParsedDocument, ParsedHeading

_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParsingFailed("Could not decode text content with any supported encoding.")


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


class TextParser:
    """Handles text/plain -- no heading extraction, since plain text has no markup."""

    def parse(self, content: bytes) -> ParsedDocument:
        text = _normalize_line_endings(decode_text(content))
        return ParsedDocument(text=text, metadata={"parser": "text", "char_count": len(text)})


class MarkdownParser:
    def parse(self, content: bytes) -> ParsedDocument:
        text = _normalize_line_endings(decode_text(content))
        headings = tuple(
            ParsedHeading(
                text=match.group(2).strip(),
                level=len(match.group(1)),
                character_offset=match.start(),
            )
            for match in _HEADING_PATTERN.finditer(text)
        )
        return ParsedDocument(
            text=text,
            headings=headings,
            metadata={
                "parser": "markdown",
                "char_count": len(text),
                "heading_count": len(headings),
            },
        )
