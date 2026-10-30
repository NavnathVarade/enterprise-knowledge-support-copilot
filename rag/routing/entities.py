"""Lightweight entity extraction: version references, quoted terms, identifiers."""

from __future__ import annotations

import re

from rag.routing.base import ExtractedEntities

_VERSION = re.compile(r"\b(?:v|version\s+)(\d{1,4})\b", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“']([^\"”']{2,200})[\"”']")
_IDENTIFIER = re.compile(r"\b[A-Z]{2,10}-\d{2,8}\b")


def extract_entities(text: str) -> ExtractedEntities:
    versions = tuple(dict.fromkeys(int(m.group(1)) for m in _VERSION.finditer(text)))
    quoted = tuple(m.group(1).strip() for m in _QUOTED.finditer(text))
    identifiers = tuple(dict.fromkeys(_IDENTIFIER.findall(text)))
    return ExtractedEntities(versions, quoted, identifiers)
