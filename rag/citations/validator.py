"""
Citation validation (Section 33, 35). Runs on the model's output BEFORE anything is
shown. A citation is valid only if its id maps to a source that was actually in the
context package. Checks:
  - every cited id exists in the provided sources (no fabricated citations)
  - a non-abstaining answer cites at least one source
  - optionally, every substantial paragraph cites something

What this does NOT prove: that the cited source actually SUPPORTS the sentence. That
needs an entailment check or LLM judge (Phase 28 evaluation) -- NOT BUILT.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from packages.domain.errors import CitationValidationFailed

from rag.citations.source_ref import SourceRef

_MARKER = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")
_ID = re.compile(r"S\d+")


@dataclass(frozen=True)
class Citation:
    source_id: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    document_version: int
    heading_path: str | None
    page_number: int | None
    source_uri: str | None


def extract_cited_ids(text: str) -> list[str]:
    """Cited ids in order of first appearance, de-duplicated."""
    seen: dict[str, None] = {}
    for match in _MARKER.finditer(text):
        for sid in _ID.findall(match.group(1)):
            seen.setdefault(sid, None)
    return list(seen)


def validate_citations(
    text: str, refs: list[SourceRef], *, require_per_paragraph: bool = False
) -> list[Citation]:
    by_id = {r.source_id: r for r in refs}
    cited = extract_cited_ids(text)

    unknown = [sid for sid in cited if sid not in by_id]
    if unknown:
        raise CitationValidationFailed(reason="unknown_source_id", unknown_ids=unknown)
    if not cited:
        raise CitationValidationFailed(reason="no_citations")

    if require_per_paragraph:
        for paragraph in (p.strip() for p in re.split(r"\n\s*\n", text)):
            if len(paragraph) >= 40 and not _MARKER.search(paragraph):
                raise CitationValidationFailed(reason="uncited_paragraph")

    citations = []
    for sid in cited:
        c = by_id[sid].chunk
        citations.append(
            Citation(
                source_id=sid,
                chunk_id=c.chunk_id,
                document_id=c.document_id,
                document_title=c.document_title,
                document_version=c.document_version,
                heading_path=c.heading_path,
                page_number=c.page_number,
                source_uri=c.source_uri,
            )
        )
    return citations
