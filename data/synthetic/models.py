"""
Pure data models for the synthetic enterprise dataset (Section 173-175).

These are deliberately decoupled from `packages.database.models` -- the generator that
produces them (generator.py) has no database dependency and is fully unit-testable for
determinism and content properties. `scripts/seed_database.py` is what maps these onto
the real ORM models and repository functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SyntheticUser:
    email: str
    display_name: str
    department: str
    password: str
    role_names: tuple[str, ...]
    group_names: tuple[str, ...]


@dataclass(frozen=True)
class SyntheticDocumentVersion:
    content: str
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    publish: bool = True  # False = leave as an unpublished draft (Section 174 case)


@dataclass(frozen=True)
class SyntheticDocumentACL:
    visibility: str  # matches DocumentVisibility values
    department: str | None = None
    allowed_user_email: str | None = None  # resolved to a user_id at seed time
    allowed_group_name: str | None = None
    allowed_role_name: str | None = None


@dataclass(frozen=True)
class SyntheticDocument:
    title: str
    source_type: str
    mime_type: str
    department: str | None
    security_classification: str
    versions: tuple[SyntheticDocumentVersion, ...]
    acl: tuple[SyntheticDocumentACL, ...] = field(default_factory=tuple)
    owner_email: str | None = None
    # A free-text tag for tests/tooling to find specific synthetic scenarios without
    # parsing titles (e.g. "prompt_injection", "conflicting_policy_a", "stale_archived").
    scenario_tag: str | None = None


@dataclass(frozen=True)
class SyntheticTenant:
    name: str
    slug: str
    users: tuple[SyntheticUser, ...]
    documents: tuple[SyntheticDocument, ...]
    role_names: tuple[str, ...]
    group_names: tuple[str, ...]


@dataclass(frozen=True)
class SyntheticDataset:
    seed: int
    tenants: tuple[SyntheticTenant, ...]
