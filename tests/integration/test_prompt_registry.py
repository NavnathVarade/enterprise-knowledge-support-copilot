from __future__ import annotations

import pytest
from packages.config.settings import get_settings
from packages.database.repositories.prompts import register_prompt
from packages.database.session import get_sessionmaker
from packages.domain.errors import ConfigurationError
from rag.generation.prompts import ALL_PROMPTS, PromptTemplate
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


@pytest.fixture
async def db_session() -> AsyncSession:
    async with get_sessionmaker(get_settings().database)() as session:
        yield session
        await session.rollback()


async def test_registration_is_idempotent_and_immutable(db_session: AsyncSession) -> None:
    p = PromptTemplate("reg_test", "1", "original text")
    a = await register_prompt(
        db_session, name=p.name, version=p.version, template_hash=p.template_hash, author="t"
    )
    b = await register_prompt(
        db_session, name=p.name, version=p.version, template_hash=p.template_hash, author="t"
    )
    assert a.id == b.id
    edited = PromptTemplate("reg_test", "1", "silently edited text")
    with pytest.raises(ConfigurationError):
        await register_prompt(
            db_session,
            name=edited.name,
            version=edited.version,
            template_hash=edited.template_hash,
            author="t",
        )
    bumped = PromptTemplate("reg_test", "2", "silently edited text")
    await register_prompt(
        db_session,
        name=bumped.name,
        version=bumped.version,
        template_hash=bumped.template_hash,
        author="t",
    )


async def test_shipped_prompts_register(db_session: AsyncSession) -> None:
    for p in ALL_PROMPTS:
        row = await register_prompt(
            db_session,
            name=p.name,
            version=p.version,
            template_hash=p.template_hash,
            author="system",
        )
        assert row.template_hash == p.template_hash
