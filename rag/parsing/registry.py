"""
Parser registry (Section 17).

A single lookup point from MIME type to `Parser` implementation, so adding a new
supported format means adding one line here plus a new parser module -- never touching
the ingestion route or any other caller.
"""

from __future__ import annotations

from packages.domain.errors import UnsupportedMimeType

from rag.parsing.base import Parser
from rag.parsing.csv_parser import CsvParser
from rag.parsing.docx_parser import DocxParser
from rag.parsing.json_parser import JsonParser
from rag.parsing.pdf_parser import PdfParser
from rag.parsing.text_parser import MarkdownParser, TextParser

_REGISTRY: dict[str, Parser] = {
    "text/plain": TextParser(),
    "text/markdown": MarkdownParser(),
    "text/csv": CsvParser(),
    "application/json": JsonParser(),
    "application/pdf": PdfParser(),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocxParser(),
}


def get_parser_for_mime_type(mime_type: str) -> Parser:
    parser = _REGISTRY.get(mime_type)
    if parser is None:
        raise UnsupportedMimeType(f"No parser registered for MIME type '{mime_type}'.")
    return parser
