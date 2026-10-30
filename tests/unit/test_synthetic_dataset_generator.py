from __future__ import annotations

import pytest
from data.synthetic.generator import generate_dataset


@pytest.mark.unit
def test_generation_is_deterministic() -> None:
    a = generate_dataset(seed=1337)
    b = generate_dataset(seed=1337)
    assert a == b  # dataclasses compare by value


@pytest.mark.unit
def test_generation_has_no_wall_clock_dependency() -> None:
    """Calling twice with no explicit time gap must still be byte-identical."""
    a = generate_dataset()
    b = generate_dataset()
    assert a == b


@pytest.mark.unit
def test_dataset_has_two_tenants_with_distinct_slugs() -> None:
    dataset = generate_dataset()
    assert len(dataset.tenants) == 2
    slugs = {t.slug for t in dataset.tenants}
    assert len(slugs) == 2


@pytest.mark.unit
@pytest.mark.parametrize("tenant_index", [0, 1])
def test_tenant_has_all_required_personas(tenant_index: int) -> None:
    dataset = generate_dataset()
    tenant = dataset.tenants[tenant_index]
    emails = {u.email for u in tenant.users}
    assert emails == {
        "alice@example.com",
        "bob@example.com",
        "john@example.com",
        "carol@example.com",
        "admin@example.com",
        "security-auditor@example.com",
    }


@pytest.mark.unit
@pytest.mark.parametrize("tenant_index", [0, 1])
def test_tenant_has_all_required_scenario_documents(tenant_index: int) -> None:
    dataset = generate_dataset()
    tenant = dataset.tenants[tenant_index]
    tags = {d.scenario_tag for d in tenant.documents}
    assert tags == {
        "versioning",
        "conflicting_policy_a",
        "conflicting_policy_b",
        "restricted_role",
        "restricted_user",
        "public_engineering",
        "stale_archived",
        "duplicate_a",
        "duplicate_b",
        "prompt_injection",
    }


@pytest.mark.unit
def test_versioning_document_has_two_versions_with_different_content() -> None:
    dataset = generate_dataset()
    tenant = dataset.tenants[0]
    doc = next(d for d in tenant.documents if d.scenario_tag == "versioning")
    assert len(doc.versions) == 2
    assert doc.versions[0].content != doc.versions[1].content
    assert doc.versions[0].effective_from is not None
    assert doc.versions[1].effective_from is not None
    assert doc.versions[0].effective_from < doc.versions[1].effective_from


@pytest.mark.unit
def test_conflicting_policies_have_different_numeric_claims() -> None:
    dataset = generate_dataset()
    tenant = dataset.tenants[0]
    doc_a = next(d for d in tenant.documents if d.scenario_tag == "conflicting_policy_a")
    doc_b = next(d for d in tenant.documents if d.scenario_tag == "conflicting_policy_b")
    assert "30 days" in doc_a.versions[0].content
    assert "14 days" in doc_b.versions[0].content
    assert doc_a.versions[0].content != doc_b.versions[0].content


@pytest.mark.unit
def test_duplicate_documents_have_identical_version_content() -> None:
    dataset = generate_dataset()
    tenant = dataset.tenants[0]
    doc_a = next(d for d in tenant.documents if d.scenario_tag == "duplicate_a")
    doc_b = next(d for d in tenant.documents if d.scenario_tag == "duplicate_b")
    assert doc_a.versions[0].content == doc_b.versions[0].content
    assert doc_a.title != doc_b.title  # different documents, same content


@pytest.mark.unit
def test_restricted_documents_carry_nonpublic_acl() -> None:
    dataset = generate_dataset()
    tenant = dataset.tenants[0]
    role_doc = next(d for d in tenant.documents if d.scenario_tag == "restricted_role")
    user_doc = next(d for d in tenant.documents if d.scenario_tag == "restricted_user")
    assert role_doc.acl[0].visibility == "role_based"
    assert role_doc.acl[0].allowed_role_name == "security_auditor"
    assert user_doc.acl[0].visibility == "user_specific"
    assert user_doc.acl[0].allowed_user_email == "admin@example.com"


@pytest.mark.unit
def test_prompt_injection_document_contains_instruction_override_attempt() -> None:
    dataset = generate_dataset()
    tenant = dataset.tenants[0]
    doc = next(d for d in tenant.documents if d.scenario_tag == "prompt_injection")
    content = doc.versions[0].content
    assert "ignore all previous instructions" in content.lower()
    assert doc.security_classification == "INTERNAL"  # visible enough to actually be retrieved


@pytest.mark.unit
def test_two_tenants_have_different_conflicting_policy_numbers() -> None:
    """Acme and Globex should not be literally identical datasets."""
    dataset = generate_dataset()
    acme_doc = next(
        d for d in dataset.tenants[0].documents if d.scenario_tag == "conflicting_policy_a"
    )
    globex_doc = next(
        d for d in dataset.tenants[1].documents if d.scenario_tag == "conflicting_policy_a"
    )
    assert acme_doc.versions[0].content != globex_doc.versions[0].content
