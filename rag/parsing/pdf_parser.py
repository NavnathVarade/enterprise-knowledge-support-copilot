"""
PDF parser -- real extraction via pypdf, not a stub.

Preserves per-page character ranges (Section 20) so a chunk built from this text can
later report which PDF page it came from, and an encrypted PDF is explicitly rejected
rather than silently returning empty text.
"""

from __future__ import annotations

import io

from packages.domain.errors import ParsingFailed
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from rag.parsing.base import ParsedDocument, ParsedPage


class PdfParser:
    def parse(self, content: bytes) -> ParsedDocument:
        try:
            reader = PdfReader(io.BytesIO(content))
        except PdfReadError as exc:
            raise ParsingFailed(f"Could not read PDF: {exc}") from exc

        if reader.is_encrypted:
            raise ParsingFailed("Encrypted PDFs are not supported.")

        if len(reader.pages) == 0:
            raise ParsingFailed("PDF contains no pages.")

        text_parts: list[str] = []
        pages: list[ParsedPage] = []
        cursor = 0

        for page_index, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 -- pypdf can raise various internal errors
                raise ParsingFailed(
                    f"Failed to extract text from page {page_index}: {exc}"
                ) from exc

            start = cursor
            text_parts.append(page_text)
            cursor += len(page_text)
            pages.append(
                ParsedPage(page_number=page_index, character_start=start, character_end=cursor)
            )
            text_parts.append("\n\n")
            cursor += 2

        full_text = "".join(text_parts)

        return ParsedDocument(
            text=full_text,
            pages=tuple(pages),
            metadata={"parser": "pdf", "page_count": len(reader.pages)},
        )
