"""Loads an evaluation corpus into a FRESH tenant through the real chunk/embed path."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from packages.database.models import Tenant
from packages.database.repositories.chunks import create_chunks_for_version
from packages.database.repositories.documents import (
    create_acl_entry,
    create_document,
    create_draft_version,
    publish_version,
)
from packages.database.repositories.embeddings import create_embeddings_for_chunks
from packages.domain.hashing import sha256_hex_of_text
from sqlalchemy.ext.asyncio import AsyncSession

from rag.chunking.chunker import chunk_document
from rag.embeddings.base import EmbeddingProvider
from rag.evaluation.dataset import CorpusDocument
from rag.parsing.text_parser import MarkdownParser


@dataclass(frozen=True)
class LoadedCorpus:
    tenant_id: uuid.UUID
    document_ids: dict[str, uuid.UUID]  # corpus key -> document id

    def key_for(self, document_id: uuid.UUID) -> str | None:
        for k, v in self.document_ids.items():
            if v == document_id:
                return k
        return None


async def load_corpus_into_new_tenant(
    session: AsyncSession,
    documents: list[CorpusDocument],
    embedding_provider: EmbeddingProvider,
    *,
    embedding_version: str = "eval",
) -> LoadedCorpus:
    """Creates a throwaway tenant. Restricted documents get NO ACL entry (fail-closed),
    so a leaked restricted chunk is a genuine authorization failure, not a data quirk."""
    tenant = Tenant(name="eval", slug=f"eval-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    ids: dict[str, uuid.UUID] = {}
    parser = MarkdownParser()
    for d in documents:
        doc = await create_document(
            session,
            tenant_id=tenant.id,
            title=d.title,
            source_type="eval",
            mime_type="text/markdown",
            content_hash=sha256_hex_of_text(d.text),
        )
        if not d.restricted:
            await create_acl_entry(
                session, tenant_id=tenant.id, document_id=doc.id, visibility="public_within_tenant"
            )
        version = await create_draft_version(
            session,
            tenant_id=tenant.id,
            document_id=doc.id,
            content_hash=sha256_hex_of_text(d.text),
        )
        version = await publish_version(
            session, tenant_id=tenant.id, document_id=doc.id, version_number=version.version
        )
        chunks = chunk_document(parser.parse(d.text.encode("utf-8")))
        rows = await create_chunks_for_version(
            session, tenant_id=tenant.id, document_version_id=version.id, chunks=chunks
        )
        if rows:
            await create_embeddings_for_chunks(
                session,
                tenant_id=tenant.id,
                chunk_ids=[r.id for r in rows],
                vectors=await embedding_provider.embed_batch([r.text for r in rows]),
                embedding_model=embedding_provider.model_name,
                embedding_version=embedding_version,
                provider=type(embedding_provider).__name__,
            )
        ids[d.key] = doc.id
    await session.commit()
    return LoadedCorpus(tenant.id, ids)
