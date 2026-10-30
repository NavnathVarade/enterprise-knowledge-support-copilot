"""
JSON parser (Section 17).

Pretty-prints valid JSON (2-space indent) so the rendered text is readable for
chunking/LLM consumption rather than a single unreadable line. Invalid JSON is a
data-quality failure (Section 113), not something to guess at.
"""

from __future__ import annotations

import json

from packages.domain.errors import ParsingFailed

from rag.parsing.base import ParsedDocument
from rag.parsing.text_parser import decode_text


class JsonParser:
    def parse(self, content: bytes) -> ParsedDocument:
        text = decode_text(content)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParsingFailed(f"Malformed JSON content: {exc}") from exc

        rendered_text = json.dumps(parsed, indent=2, ensure_ascii=False)
        top_level_type = type(parsed).__name__
        key_count = len(parsed) if isinstance(parsed, dict | list) else None

        return ParsedDocument(
            text=rendered_text,
            metadata={
                "parser": "json",
                "top_level_type": top_level_type,
                "top_level_length": key_count,
            },
        )
