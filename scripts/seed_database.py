"""
Seeds the database with the deterministic synthetic dataset (Section 173-176).

Idempotent by design: re-running this script must not create duplicate tenants, users,
roles, groups, or documents. Every repository call used here is a "get or create" (for
tenants/roles/groups/users) or checks for an existing document by (tenant_id, title)
before creating (for documents) -- see `tests/integration/test_seed_database.py::
test_seeding_twice_is_idempotent` for the proof.

Usage:
    uv run python -m scripts.seed_database
"""

from __future__ import annotations

import asyncio
import uuid

from data.synthetic.generator import generate_dataset
from data.synthetic.models import SyntheticDataset, SyntheticDocument, SyntheticTenant
from packages.config.settings import Environment, get_settings
from packages.database.repositories.documents import (
    advance_document_to_active,
    create_acl_entry,
    create_document,
    create_draft_version,
    get_document_by_title,
    publish_version,
    transition_document_status,
)
from packages.database.repositories.identity import (
    assign_group_to_user,
    assign_role_to_user,
    get_or_create_group,
    get_or_create_role,
    get_or_create_tenant,
    get_or_create_user,
    get_user_by_tenant_and_email,
)
from packages.database.session import get_sessionmaker
from packages.domain.hashing import sha256_hex_of_text
from packages.observability.logging import get_logger
from packages.security.passwords import hash_password
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger()


async def _seed_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    doc: SyntheticDocument,
    email_to_user_id: dict[str, uuid.UUID],
    role_name_to_id: dict[str, uuid.UUID],
    group_name_to_id: dict[str, uuid.UUID],
) -> None:
    existing = await get_document_by_title(session, tenant_id=tenant_id, title=doc.title)
    if existing is not None:
        logger.info("seed_document_skipped_existing", title=doc.title)
        return

    owner_id = email_to_user_id.get(doc.owner_email) if doc.owner_email else None
    first_version_hash = sha256_hex_of_text(doc.versions[0].content)

    document = await create_document(
        session,
        tenant_id=tenant_id,
        title=doc.title,
        source_type=doc.source_type,
        mime_type=doc.mime_type,
        content_hash=first_version_hash,
        department=doc.department,
        security_classification=doc.security_classification,
        owner_id=owner_id,
    )

    for synthetic_version in doc.versions:
        version_hash = sha256_hex_of_text(synthetic_version.content)
        version = await create_draft_version(
            session, tenant_id=tenant_id, document_id=document.id, content_hash=version_hash
        )
        if synthetic_version.publish:
            await publish_version(
                session,
                tenant_id=tenant_id,
                document_id=document.id,
                version_number=version.version,
                effective_from=synthetic_version.effective_from,
            )

    for synthetic_acl in doc.acl:
        allowed_user_id = (
            email_to_user_id.get(synthetic_acl.allowed_user_email)
            if synthetic_acl.allowed_user_email
            else None
        )
        allowed_group_id = (
            group_name_to_id.get(synthetic_acl.allowed_group_name)
            if synthetic_acl.allowed_group_name
            else None
        )
        allowed_role_id = (
            role_name_to_id.get(synthetic_acl.allowed_role_name)
            if synthetic_acl.allowed_role_name
            else None
        )
        await create_acl_entry(
            session,
            tenant_id=tenant_id,
            document_id=document.id,
            visibility=synthetic_acl.visibility,
            department=synthetic_acl.department,
            allowed_user_id=allowed_user_id,
            allowed_group_id=allowed_group_id,
            allowed_role_id=allowed_role_id,
        )

    await advance_document_to_active(session, tenant_id=tenant_id, document_id=document.id)

    if doc.scenario_tag == "stale_archived":
        await transition_document_status(
            session, tenant_id=tenant_id, document_id=document.id, target_status="archived"
        )

    logger.info("seed_document_created", title=doc.title, scenario_tag=doc.scenario_tag)


async def _seed_tenant(session: AsyncSession, tenant_data: SyntheticTenant) -> None:
    tenant = await get_or_create_tenant(session, name=tenant_data.name, slug=tenant_data.slug)

    role_name_to_id: dict[str, uuid.UUID] = {}
    for role_name in tenant_data.role_names:
        role = await get_or_create_role(session, tenant_id=tenant.id, name=role_name)
        role_name_to_id[role_name] = role.id

    group_name_to_id: dict[str, uuid.UUID] = {}
    for group_name in tenant_data.group_names:
        group = await get_or_create_group(session, tenant_id=tenant.id, name=group_name)
        group_name_to_id[group_name] = group.id

    email_to_user_id: dict[str, uuid.UUID] = {}
    for synthetic_user in tenant_data.users:
        existing_user = await get_user_by_tenant_and_email(
            session, tenant_id=tenant.id, email=synthetic_user.email
        )
        if existing_user is not None:
            user = existing_user
        else:
            user = await get_or_create_user(
                session,
                tenant_id=tenant.id,
                email=synthetic_user.email,
                display_name=synthetic_user.display_name,
                hashed_password=hash_password(synthetic_user.password),
                department=synthetic_user.department,
            )
        email_to_user_id[synthetic_user.email] = user.id

        for role_name in synthetic_user.role_names:
            await assign_role_to_user(
                session, tenant_id=tenant.id, user_id=user.id, role_id=role_name_to_id[role_name]
            )
        for group_name in synthetic_user.group_names:
            await assign_group_to_user(
                session,
                tenant_id=tenant.id,
                user_id=user.id,
                group_id=group_name_to_id[group_name],
            )

    for doc in tenant_data.documents:
        await _seed_document(
            session,
            tenant_id=tenant.id,
            doc=doc,
            email_to_user_id=email_to_user_id,
            role_name_to_id=role_name_to_id,
            group_name_to_id=group_name_to_id,
        )

    logger.info("seed_tenant_complete", tenant=tenant_data.name, slug=tenant_data.slug)


class SeedingRefused(RuntimeError):
    pass


async def seed_database(dataset: SyntheticDataset | None = None) -> None:
    dataset = dataset or generate_dataset()
    settings = get_settings()
    # The synthetic users (including an admin) have fixed, publicly documented passwords.
    # Creating them in a real environment would be a ready-made backdoor.
    if settings.environment not in (Environment.development, Environment.test):
        raise SeedingRefused(
            "Synthetic seeding creates users with known passwords and is only allowed when "
            f"ENVIRONMENT is development or test (got {settings.environment.value})."
        )
    sessionmaker = get_sessionmaker(settings.database)

    async with sessionmaker() as session:
        for tenant_data in dataset.tenants:
            await _seed_tenant(session, tenant_data)
        await session.commit()

    logger.info("seed_database_complete", tenant_count=len(dataset.tenants))


if __name__ == "__main__":
    asyncio.run(seed_database())
