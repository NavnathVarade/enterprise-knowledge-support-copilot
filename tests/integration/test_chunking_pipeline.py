"""
End-to-end proof that Phase 9's parsers, Phase 10's chunker, and the chunk repository
compose correctly against a real database: parse a genuinely generated PDF, chunk it,
persist the chunks tied to a real document_version, and verify the full lineage chain
(Section 21) plus tenant isolation at the chunk level.
"""

from __future__ import annotations

import io
import uuid

import pytest
from packages.config.settings import get_settings
from packages.database.models import Tenant
from packages.database.repositories.chunks import create_chunks_for_version, get_chunks_for_version
from packages.database.repositories.documents import (
    create_document,
    create_draft_version,
    publish_version,
)
from packages.database.session import get_sessionmaker
from packages.domain.hashing import sha256_hex_of_text
from rag.chunking.chunker import chunk_document
from rag.parsing.pdf_parser import PdfParser
from reportlab.pdfgen import canvas
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def db_session() -> AsyncSession:
    settings = get_settings()
    sessionmaker = get_sessionmaker(settings.database)
    async with sessionmaker() as session:
        yield session
        await session.rollback()


async def _make_tenant(session: AsyncSession, name: str) -> Tenant:
    tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


def _make_multi_page_pdf() -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    # Enough text per page that a modest token budget naturally forces a chunk split
    # at the page boundary -- the chunker does NOT hard-enforce "never span pages"
    # the way it hard-enforces heading boundaries (documented gap, see
    # docs/rag/PHASE_10_CHUNKING_DESIGN.md), so this test relies on the token budget
    # rather than asserting a guarantee that doesn't exist.
    c.drawString(72, 720, "First page of the real parsing pipeline test with more words.")
    c.drawString(72, 700, "Additional content on page one to ensure it exceeds the budget.")
    c.showPage()
    c.drawString(72, 720, "Second page with different content entirely and also more words.")
    c.drawString(72, 700, "Additional content on page two to ensure it exceeds the budget.")
    c.showPage()
    c.save()
    return buffer.getvalue()


@pytest.mark.integration
async def test_full_parse_chunk_persist_pipeline(db_session: AsyncSession) -> None:
    tenant = await _make_tenant(db_session, "PipelineCo")

    pdf_bytes = _make_multi_page_pdf()
    parsed = PdfParser().parse(pdf_bytes)
    chunks = chunk_document(parsed, target_tokens=15, overlap_tokens=0)
    assert len(chunks) >= 1

    document = await create_document(
        db_session,
        tenant_id=tenant.id,
        title="Pipeline Test PDF",
        source_type="upload",
        mime_type="application/pdf",
        content_hash=sha256_hex_of_text(parsed.text),
    )
    version = await create_draft_version(
        db_session,
        tenant_id=tenant.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text(parsed.text),
    )
    version = await publish_version(
        db_session, tenant_id=tenant.id, document_id=document.id, version_number=version.version
    )

    persisted = await create_chunks_for_version(
        db_session, tenant_id=tenant.id, document_version_id=version.id, chunks=chunks
    )
    await db_session.commit()

    assert len(persisted) == len(chunks)
    for row in persisted:
        assert row.chunking_version == "v1"
        assert len(row.content_hash) == 64
        assert row.document_version_id == version.id

    reloaded = await get_chunks_for_version(
        db_session, tenant_id=tenant.id, document_version_id=version.id
    )
    assert [c.sequence for c in reloaded] == sorted(c.sequence for c in reloaded)
    assert {c.text for c in reloaded} == {c.text for c in chunks}

    page_numbers = {c.page_number for c in reloaded}
    assert page_numbers == {1, 2}


@pytest.mark.integration
@pytest.mark.security
async def test_chunks_are_tenant_isolated_at_the_schema_level(db_session: AsyncSession) -> None:
    tenant_a = await _make_tenant(db_session, "ChunkTenantA")
    tenant_b = await _make_tenant(db_session, "ChunkTenantB")

    document = await create_document(
        db_session,
        tenant_id=tenant_a.id,
        title="Tenant A Doc",
        source_type="upload",
        mime_type="text/plain",
        content_hash=sha256_hex_of_text("content"),
    )
    version = await create_draft_version(
        db_session,
        tenant_id=tenant_a.id,
        document_id=document.id,
        content_hash=sha256_hex_of_text("c"),
    )
    version = await publish_version(
        db_session, tenant_id=tenant_a.id, document_id=document.id, version_number=version.version
    )

    from packages.database.models.documents import DocumentChunk

    malicious_chunk = DocumentChunk(
        tenant_id=tenant_b.id,
        document_version_id=version.id,
        sequence=0,
        text="leaked content",
        token_count=3,
        char_start=0,
        char_end=14,
        content_hash=sha256_hex_of_text("leaked content"),
        chunking_version="v1",
    )
    db_session.add(malicious_chunk)

    with pytest.raises(IntegrityError, match="fk_document_chunks_document_version"):
        await db_session.flush()


@pytest.mark.integration
async def test_create_chunks_for_nonexistent_version_is_rejected(db_session: AsyncSession) -> None:
    from packages.domain.errors import DocumentNotFound

    tenant = await _make_tenant(db_session, "NoVersionCo")
    with pytest.raises(DocumentNotFound):
        await create_chunks_for_version(
            db_session, tenant_id=tenant.id, document_version_id=uuid.uuid4(), chunks=[]
        )
