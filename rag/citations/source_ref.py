"""SourceRef: a context chunk with the id the model cites it by. Owned by citations (which validates
references to it); generation builds and renders them."""

from __future__ import annotations

from dataclasses import dataclass

from rag.context.base import ContextChunk


@dataclass(frozen=True)
class SourceRef:
    source_id: str  # "S1", "S2", ...
    chunk: ContextChunk
