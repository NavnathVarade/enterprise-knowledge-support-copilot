"""
FastAPI dependencies for authentication and RBAC.

`get_current_identity` is the ONLY place a request's tenant_id enters the rest of the
application (Section 9). Every route that touches tenant-scoped data must depend on it
(directly or via `require_role`) rather than reading a tenant_id from a path/query
parameter or request body.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from packages.auth.identity import Identity
from packages.auth.jwt import InvalidToken, verify_access_token
from packages.domain.errors import DomainError

_bearer_scheme = HTTPBearer(auto_error=False)


class MissingCredentials(DomainError):
    code = "MISSING_CREDENTIALS"
    http_status = 401
    safe_message = "Authentication credentials were not provided."


class InsufficientPermissions(DomainError):
    code = "INSUFFICIENT_PERMISSIONS"
    http_status = 403
    safe_message = "You do not have permission to perform this action."


async def get_current_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Identity:
    if credentials is None:
        raise MissingCredentials()
    settings = request.app.state.settings
    try:
        return verify_access_token(credentials.credentials, settings=settings.security)
    except InvalidToken:
        raise


def require_role(*allowed_roles: str) -> Callable[[Identity], Identity]:
    """
    Dependency factory: `Depends(require_role("admin", "security_auditor"))`.

    Kept separate from `get_current_identity` so routes that need no particular role
    still only depend on authentication, and so the *set* of roles required by a route
    is visible in its signature rather than buried in handler body logic.
    """

    def _check(identity: Identity = Depends(get_current_identity)) -> Identity:
        if not identity.has_any_role(*allowed_roles):
            raise InsufficientPermissions()
        return identity

    return _check
