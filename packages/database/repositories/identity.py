"""
Identity-domain read queries used by the auth flow.

`get_user_with_roles_and_groups` is deliberately the only place that resolves a user's
role/group names -- callers get back plain string sets ready to embed in a JWT or check
against `require_role`, without needing to know the join shape.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.database.models import Group, Role, Tenant, User, UserGroup, UserRole


async def get_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    result = await session.execute(select(Tenant).where(Tenant.slug == slug))
    return result.scalar_one_or_none()


async def get_or_create_tenant(session: AsyncSession, *, name: str, slug: str) -> Tenant:
    existing = await get_tenant_by_slug(session, slug)
    if existing is not None:
        return existing
    tenant = Tenant(name=name, slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def get_or_create_role(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> Role:
    result = await session.execute(
        select(Role).where(Role.tenant_id == tenant_id, Role.name == name)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing
    role = Role(tenant_id=tenant_id, name=name)
    session.add(role)
    await session.flush()
    return role


async def get_or_create_group(session: AsyncSession, *, tenant_id: uuid.UUID, name: str) -> Group:
    result = await session.execute(
        select(Group).where(Group.tenant_id == tenant_id, Group.name == name)
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing
    group = Group(tenant_id=tenant_id, name=name)
    session.add(group)
    await session.flush()
    return group


async def get_or_create_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    display_name: str,
    hashed_password: str,
    department: str | None = None,
) -> User:
    existing = await get_user_by_tenant_and_email(session, tenant_id=tenant_id, email=email)
    if existing is not None:
        return existing
    user = User(
        tenant_id=tenant_id,
        email=email,
        display_name=display_name,
        hashed_password=hashed_password,
        department=department,
    )
    session.add(user)
    await session.flush()
    return user


async def assign_role_to_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
) -> None:
    result = await session.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
    )
    if result.scalar_one_or_none() is not None:
        return
    session.add(UserRole(tenant_id=tenant_id, user_id=user_id, role_id=role_id))
    await session.flush()


async def assign_group_to_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, group_id: uuid.UUID
) -> None:
    result = await session.execute(
        select(UserGroup).where(UserGroup.user_id == user_id, UserGroup.group_id == group_id)
    )
    if result.scalar_one_or_none() is not None:
        return
    session.add(UserGroup(tenant_id=tenant_id, user_id=user_id, group_id=group_id))
    await session.flush()


async def get_user_by_tenant_and_email(
    session: AsyncSession, *, tenant_id: uuid.UUID, email: str
) -> User | None:
    result = await session.execute(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    )
    return result.scalar_one_or_none()


async def get_role_names_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> frozenset[str]:
    result = await session.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    return frozenset(result.scalars().all())


async def get_group_names_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> frozenset[str]:
    result = await session.execute(
        select(Group.name)
        .join(UserGroup, UserGroup.group_id == Group.id)
        .where(UserGroup.user_id == user_id)
    )
    return frozenset(result.scalars().all())
