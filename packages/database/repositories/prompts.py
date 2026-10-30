"""Prompt registration (Section 36): idempotent, and a stored (name, version) is
immutable. Re-registering identical text is a no-op; different text under the same
version is an error -- the author must bump the version."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models.prompts import PromptVersion
from packages.domain.errors import ConfigurationError


async def register_prompt(
    session: AsyncSession, *, name: str, version: str, template_hash: str, author: str
) -> PromptVersion:
    existing = (
        await session.execute(
            select(PromptVersion).where(
                PromptVersion.name == name, PromptVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.template_hash != template_hash:
            raise ConfigurationError(
                f"Prompt {name} v{version} already registered with different content; "
                "bump the version instead of editing it."
            )
        return existing
    row = PromptVersion(name=name, version=version, template_hash=template_hash, author=author)
    session.add(row)
    await session.flush()
    return row
