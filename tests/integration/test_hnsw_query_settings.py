"""RETRIEVAL_HNSW_* settings reach Postgres, stay transaction-local, and validate."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from packages.config.settings import RetrievalSettings, get_settings
from packages.database.repositories.embeddings import apply_hnsw_query_settings
from packages.database.session import get_sessionmaker
from pydantic import ValidationError
from sqlalchemy import text


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _show(session, name: str) -> str:  # type: ignore[no-untyped-def]
    # hnsw.* settings exist only once the pgvector library is loaded in this backend.
    await session.execute(text("select '[1]'::vector"))
    return str((await session.execute(text(f"show {name}"))).scalar_one())


@pytest.mark.integration
async def test_ef_search_is_applied_and_transaction_local(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("RETRIEVAL_HNSW_EF_SEARCH", "123")
    sessionmaker = get_sessionmaker(get_settings().database)
    async with sessionmaker() as session:
        default = await _show(session, "hnsw.ef_search")
        await session.rollback()
        await apply_hnsw_query_settings(session)
        assert await _show(session, "hnsw.ef_search") == "123"
        await session.rollback()  # end of transaction: the setting must not leak
        assert await _show(session, "hnsw.ef_search") == default


@pytest.mark.integration
async def test_iterative_scan_is_applied_when_pgvector_supports_it(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    sessionmaker = get_sessionmaker(get_settings().database)
    async with sessionmaker() as session:
        version = (
            await session.execute(
                text("select extversion from pg_extension where extname = 'vector'")
            )
        ).scalar_one()
        await session.rollback()
    if tuple(int(x) for x in version.split(".")[:2]) < (0, 8):
        pytest.skip(f"pgvector {version} has no hnsw.iterative_scan (needs >= 0.8)")
    monkeypatch.setenv("RETRIEVAL_HNSW_ITERATIVE_SCAN", "relaxed_order")
    get_settings.cache_clear()
    async with sessionmaker() as session:
        await apply_hnsw_query_settings(session)
        assert await _show(session, "hnsw.iterative_scan") == "relaxed_order"
        await session.rollback()


@pytest.mark.integration
async def test_unset_settings_change_nothing(fresh_settings: None) -> None:
    sessionmaker = get_sessionmaker(get_settings().database)
    async with sessionmaker() as session:
        before = await _show(session, "hnsw.ef_search")
        await apply_hnsw_query_settings(session)
        assert await _show(session, "hnsw.ef_search") == before
        await session.rollback()


def test_invalid_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RetrievalSettings(hnsw_ef_search=0)
    with pytest.raises(ValidationError):
        RetrievalSettings(hnsw_ef_search=5000)
    with pytest.raises(ValidationError):
        RetrievalSettings(hnsw_iterative_scan="fast")  # type: ignore[arg-type]
