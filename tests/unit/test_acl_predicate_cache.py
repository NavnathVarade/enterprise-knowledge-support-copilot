"""The ACL predicate is memoised (Phase 45). A cache that ignored an input would be an authorization
leak, so these tests pin: equal inputs share one object, and changing ANY input changes the SQL params."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from packages.auth.identity import Identity
from packages.database.repositories import authorization as authz
from sqlalchemy.dialects import postgresql

pytestmark = pytest.mark.security

T = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BASE = Identity(
    user_id=uuid.UUID("00000000-0000-0000-0000-0000000000b1"),
    tenant_id=T,
    email="a@example.com",
    roles=frozenset({"analyst"}),
    groups=frozenset({"support"}),
    department="finance",
)


def _compiled(pred: object) -> tuple[str, dict[str, object]]:
    c = pred.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return str(c), dict(c.params)


def _uncached(tenant: uuid.UUID, i: Identity) -> tuple[str, dict[str, object]]:
    return _compiled(authz._build_predicate(tenant, i.user_id, i.department, i.groups, i.roles))


def test_cached_result_is_identical_to_a_fresh_build() -> None:
    assert _compiled(authz.build_document_acl_predicate(T, BASE)) == _uncached(T, BASE)


def test_equal_inputs_share_the_cached_object() -> None:
    twin = replace(BASE, email="different-email-is-irrelevant@example.com")
    assert authz.build_document_acl_predicate(T, BASE) is authz.build_document_acl_predicate(
        T, twin
    )


@pytest.mark.parametrize(
    "variant",
    [
        {"user_id": uuid.UUID("00000000-0000-0000-0000-0000000000b2")},
        {"department": "legal"},
        {"department": None},
        {"groups": frozenset({"support", "admins"})},
        {"groups": frozenset()},
        {"roles": frozenset({"admin"})},
        {"roles": frozenset()},
    ],
)
def test_changing_any_input_changes_the_predicate(variant: dict[str, object]) -> None:
    other = replace(BASE, **variant)  # type: ignore[arg-type]
    cached_other = _compiled(authz.build_document_acl_predicate(T, other))
    assert cached_other == _uncached(T, other)
    assert cached_other[1] != _compiled(authz.build_document_acl_predicate(T, BASE))[1]


def test_a_different_tenant_never_reuses_another_tenants_predicate() -> None:
    t2 = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
    p1 = authz.build_document_acl_predicate(T, BASE)
    p2 = authz.build_document_acl_predicate(t2, BASE)
    assert p1 is not p2 and _compiled(p1)[1] != _compiled(p2)[1]


def test_group_and_role_order_does_not_matter() -> None:
    a = replace(BASE, groups=frozenset({"x", "y", "z"}))
    b = replace(BASE, groups=frozenset({"z", "y", "x"}))
    assert authz.build_document_acl_predicate(T, a) is authz.build_document_acl_predicate(T, b)
