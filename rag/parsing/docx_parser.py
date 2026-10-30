"""
DOCX parser -- real extraction via python-docx.

Word's built-in "Heading 1".."Heading 6" paragraph styles map directly to heading level
(Section 20/22's heading-aware chunking), which is more reliable than guessing from font
size/bold -- it's exactly the structural information the document's author provided.
"""

from __future__ import annotations

import io
import re
import zipfile

import docx
from docx.opc.exceptions import PackageNotFoundError
from packages.domain.errors import ParsingFailed

from rag.parsing.base import ParsedDocument, ParsedHeading

_HEADING_STYLE_PATTERN = re.compile(r"^Heading (\d)$")


class DocxParser:
    def parse(self, content: bytes) -> ParsedDocument:
        try:
            document = docx.Document(io.BytesIO(content))
        except (PackageNotFoundError, zipfile.BadZipFile) as exc:
            # PackageNotFoundError: a valid ZIP that isn't a valid OOXML package.
            # BadZipFile: content that isn't even a ZIP container at all (e.g. a
            # plain text file misidentified as .docx) -- caught by
            # test_docx_parser_rejects_corrupted_content actually failing with an
            # unhandled BadZipFile before this except clause was added.
            raise ParsingFailed(f"Could not read DOCX file: {exc}") from exc

        if len(document.paragraphs) == 0:
            raise ParsingFailed("DOCX document contains no content.")

        text_parts: list[str] = []
        headings: list[ParsedHeading] = []
        cursor = 0

        for paragraph in document.paragraphs:
            paragraph_text = paragraph.text
            style_name = paragraph.style.name if paragraph.style else ""
            heading_match = _HEADING_STYLE_PATTERN.match(style_name or "")
            if heading_match and paragraph_text.strip():
                headings.append(
                    ParsedHeading(
                        text=paragraph_text.strip(),
                        level=int(heading_match.group(1)),
                        character_offset=cursor,
                    )
                )

            text_parts.append(paragraph_text)
            cursor += len(paragraph_text)
            text_parts.append("\n")
            cursor += 1

        full_text = "".join(text_parts)

        return ParsedDocument(
            text=full_text,
            headings=tuple(headings),
            metadata={
                "parser": "docx",
                "paragraph_count": len(document.paragraphs),
                "heading_count": len(headings),
            },
        )
