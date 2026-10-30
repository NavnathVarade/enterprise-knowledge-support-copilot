"""
Database session dependencies.

Two flavors, deliberately not interchangeable:
  - `get_db_session`: an unscoped session, usable ONLY before an Identity exists (e.g.
    resolving a tenant by slug during login). Must never be used to read/write
    tenant-scoped business data.
  - `get_tenant_session`: requires an authenticated `Identity` first and binds the
    session to that identity's tenant_id (see packages/database/session.
    tenant_scoped_session). Every route touching tenant-scoped tables should depend on
    this, not on get_db_session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from apps.api.app.dependencies.auth import get_current_identity
from fastapi import Depends, Request
from packages.auth.identity import Identity
from packages.database.session import get_sessionmaker, tenant_scoped_session
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    settings = request.app.state.settings
    sessionmaker = get_sessionmaker(settings.database)
    async with sessionmaker() as session:
        yield session


async def get_tenant_session(
    request: Request,
    identity: Identity = Depends(get_current_identity),
) -> AsyncIterator[AsyncSession]:
    settings = request.app.state.settings
    async with tenant_scoped_session(
        settings.database, tenant_id=str(identity.tenant_id)
    ) as session:
        yield session
