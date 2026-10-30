"""
Builds the user message from a ContextPackage. Source text and metadata are escaped so a
document cannot close the <source> tag and smuggle in instructions that look like they
sit outside the data block. Escaping is defense in depth, not a guarantee: the real
boundaries are authorization (Phase 16/18) and citation validation (this phase).
"""

from __future__ import annotations

from rag.citations.source_ref import SourceRef
from rag.context.base import ContextPackage

__all__ = ["SourceRef", "build_source_refs", "render_user_message"]


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def build_source_refs(package: ContextPackage) -> list[SourceRef]:
    return [SourceRef(f"S{i}", c) for i, c in enumerate(package.chunks, start=1)]


def render_user_message(question: str, refs: list[SourceRef]) -> str:
    blocks = []
    for ref in refs:
        c = ref.chunk
        attrs = [
            f'id="{ref.source_id}"',
            f'title="{_esc(c.document_title)}"',
            f'version="{c.document_version}"',
        ]
        if c.heading_path:
            attrs.append(f'section="{_esc(c.heading_path)}"')
        if c.page_number is not None:
            attrs.append(f'page="{c.page_number}"')
        attrs.append(f'classification="{_esc(c.security_classification)}"')
        blocks.append(f"<source {' '.join(attrs)}>\n{_esc(c.text)}\n</source>")
    sources = "\n".join(blocks)
    return f"<sources>\n{sources}\n</sources>\n\n<question>\n{_esc(question)}\n</question>"
