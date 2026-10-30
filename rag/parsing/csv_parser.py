"""
CSV parser (Section 17, 22).

Renders the CSV as a Markdown-style table in `text` (so it reads sensibly to an LLM
downstream) while preserving row/column counts and headers in metadata for anything
that wants structured access without re-parsing. A CSV with inconsistent column counts
per row, or that isn't valid CSV at all, is treated as a data-quality failure
(Section 113), not silently coerced.
"""

from __future__ import annotations

import csv
import io

from packages.domain.errors import ParsingFailed

from rag.parsing.base import ParsedDocument
from rag.parsing.text_parser import decode_text


class CsvParser:
    def parse(self, content: bytes) -> ParsedDocument:
        text = decode_text(content)
        try:
            reader = csv.reader(io.StringIO(text))
            rows = list(reader)
        except csv.Error as exc:
            raise ParsingFailed(f"Malformed CSV content: {exc}") from exc

        if not rows:
            raise ParsingFailed("CSV content is empty.")

        header, *data_rows = rows
        column_count = len(header)
        for row_index, row in enumerate(data_rows, start=2):  # row 1 is the header
            if len(row) != column_count:
                raise ParsingFailed(
                    f"CSV row {row_index} has {len(row)} columns, expected {column_count} "
                    f"(matching the header)."
                )

        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in data_rows)
        rendered_text = "\n".join(lines)

        return ParsedDocument(
            text=rendered_text,
            metadata={
                "parser": "csv",
                "header": header,
                "row_count": len(data_rows),
                "column_count": column_count,
            },
        )
