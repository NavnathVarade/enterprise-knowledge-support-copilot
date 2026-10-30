"""Query normalization: NFKC, control-char removal, whitespace collapse, length bound."""

from __future__ import annotations

import re
import unicodedata

from packages.domain.errors import InvalidQuery

MAX_QUERY_CHARS = 2000
_WS = re.compile(r"\s+")


def normalize_query(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw)
    text = "".join(ch for ch in text if ch.isprintable() or ch in "\t\n ")
    text = _WS.sub(" ", text).strip()
    if not text:
        raise InvalidQuery("Query is empty.")
    if len(text) > MAX_QUERY_CHARS:
        raise InvalidQuery(f"Query exceeds {MAX_QUERY_CHARS} characters.")
    return text
