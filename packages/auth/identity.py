"""
The authenticated identity extracted from a verified access token.

This is deliberately the ONLY source of tenant_id used to scope queries in the request
path (Section 9 of the architecture brief: "Never trust tenant_id from the request
body"). Nothing downstream should accept a tenant_id parameter from a request body or
query string and use it for authorization -- it must come from here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    roles: frozenset[str]
    groups: frozenset[str]
    department: str | None = None

    def has_role(self, role_name: str) -> bool:
        return role_name in self.roles

    def has_any_role(self, *role_names: str) -> bool:
        return bool(self.roles.intersection(role_names))
