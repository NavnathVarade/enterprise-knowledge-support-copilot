"""Shared construction of the ingestion handler's dependencies (used by the Kafka ingestion
worker and by the development dispatcher, so both run byte-for-byte the same pipeline)."""

from __future__ import annotations

import functools

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.config.settings import Settings
from packages.ingestion.pipeline import IngestionDependencies, handle_document_ingested
from packages.kafka.topics import Topics
from packages.providers.factory import build_embedding_provider
from packages.storage.object_store import build_object_store
from packages.usage.recorder import DbUsageRecorder
from packages.workers.local_dispatch import Handler, LocalEventDispatcher


def build_ingestion_dependencies(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> IngestionDependencies:
    return IngestionDependencies(
        sessionmaker=sessionmaker,
        store=build_object_store(settings.object_store),
        embedding_provider=build_embedding_provider(settings),
        ingestion_settings=settings.ingestion,
        key_prefix=settings.object_store.key_prefix,
        embedding_batch_size=settings.embedding.batch_size,
        usage_recorder=(
            DbUsageRecorder(settings.database) if settings.usage.metering_enabled else None
        ),
        embedding_usd_per_million_tokens=settings.embedding.usd_per_million_tokens,
    )


def build_local_dispatcher(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> LocalEventDispatcher:
    deps = build_ingestion_dependencies(settings, sessionmaker)
    handlers: dict[str, Handler] = {
        Topics.DOCUMENT_INGESTED: functools.partial(handle_document_ingested, deps),
    }
    return LocalEventDispatcher(
        sessionmaker,
        handlers,
        poll_seconds=settings.events.local_dispatch_poll_seconds,
        max_attempts=settings.events.local_dispatch_max_attempts,
        retry_base_seconds=settings.events.local_dispatch_retry_base_seconds,
    )
