"""
ACL authorization predicate (Section 11, 13).

THE central rule this module exists to enforce: "Authorization must happen before
context reaches the LLM." `build_document_acl_predicate` returns a SQL boolean
expression -- not a Python filter applied to already-fetched rows -- so an unauthorized
document's chunks are never even retrieved from Postgres, let alone handed to a
reranker or an LLM. Every retrieval query in this phase (vector, lexical, hybrid) ANDs
this predicate into its WHERE clause alongside the tenant_id filter; none of them fetch
first and filter in application code.

A document is authorized for an Identity if ANY of its document_acl rows grant access:
  - visibility='public_within_tenant': always authorized (within the tenant)
  - visibility='department_only': the row's `department` matches Identity.department
  - visibility='user_specific': the row's `allowed_user_id` matches Identity.user_id
  - visibility='group_only': the row's `allowed_group_id` resolves to a group name in
    Identity.groups
  - visibility='role_based': the row's `allowed_role_id` resolves to a role name in
    Identity.roles
  - visibility='restricted': matches NOTHING here by design -- this reference
    implementation treats it as "no automatic grant exists," so a restricted document
    is invisible to retrieval unless a DIFFERENT ACL row on the same document also
    grants access some other way. An explicit admin override path is a later-phase
    concern, not built here.
A document with NO document_acl rows at all is authorized to nobody -- the absence of a
grant is not an implicit grant (fail closed, not fail open).
"""

from __future__ import annotations

import uuid
from functools import lru_cache

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import aliased

from packages.auth.identity import Identity
from packages.database.models.documents import Document, DocumentACL, DocumentVisibility
from packages.database.models.identity import Group, Role


def build_document_acl_predicate(tenant_id: uuid.UUID, identity: Identity) -> ColumnElement[bool]:
    """
    A correlated EXISTS(...) expression, true when `identity` is authorized to see the
    `Document` row referenced by the enclosing query's (unaliased) `Document.id` /
    `Document.tenant_id` columns. The caller's query MUST already join the unaliased
    `Document` table -- this predicate correlates to it by column identity, exactly
    like a normal correlated subquery.

    Building the expression costs a few milliseconds of CPU (Phase 45 profile: ~35% of the
    platform's own per-query CPU, as it is built several times per question). SQLAlchemy
    expressions are immutable, so the result is memoised per *complete* set of inputs it
    depends on. The key is every field the expression reads; if a field is ever added to the
    predicate it MUST be added to the key (tests/unit/test_acl_predicate_cache.py compares
    cached and uncached output to catch a miss).
    """
    return _cached_predicate(
        tenant_id,
        identity.user_id,
        identity.department,
        frozenset(identity.groups),
        frozenset(identity.roles),
    )


@lru_cache(maxsize=2048)
def _cached_predicate(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    department: str | None,
    groups: frozenset[str],
    roles: frozenset[str],
) -> ColumnElement[bool]:
    return _build_predicate(tenant_id, user_id, department, groups, roles)


def _build_predicate(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    department: str | None,
    groups: frozenset[str],
    roles: frozenset[str],
) -> ColumnElement[bool]:
    acl = aliased(DocumentACL)
    group = aliased(Group)
    role = aliased(Role)

    is_public = acl.visibility == DocumentVisibility.public_within_tenant.value
    is_department_match = and_(
        acl.visibility == DocumentVisibility.department_only.value,
        acl.department == department,
    )
    is_user_match = and_(
        acl.visibility == DocumentVisibility.user_specific.value,
        acl.allowed_user_id == user_id,
    )
    is_group_match = and_(
        acl.visibility == DocumentVisibility.group_only.value,
        group.tenant_id == tenant_id,
        group.name.in_(sorted(groups)),
    )
    is_role_match = and_(
        acl.visibility == DocumentVisibility.role_based.value,
        role.tenant_id == tenant_id,
        role.name.in_(sorted(roles)),
    )

    grant_exists = (
        select(1)
        .select_from(acl)
        .outerjoin(group, acl.allowed_group_id == group.id)
        .outerjoin(role, acl.allowed_role_id == role.id)
        .where(
            acl.tenant_id == tenant_id,
            acl.document_id == Document.id,
            or_(is_public, is_department_match, is_user_match, is_group_match, is_role_match),
        )
        .exists()
    )
    return grant_exists
