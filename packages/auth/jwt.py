"""
JWT issuance and verification.

Access tokens are RS256-signed and carry tenant_id, roles, and groups as claims so that
`Identity` (packages/auth/identity.py) can be reconstructed from the token alone,
without an extra database round trip on every request. Roles/groups are therefore only
as fresh as the token's TTL (Section 77's "access revoked today" concern) -- short TTLs
(15 minutes, configurable via SECURITY_ACCESS_TOKEN_TTL_SECONDS) bound how stale that
can get, and Phase 16 adds a revocation check for anything more time-sensitive than that.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel, ValidationError

from packages.auth.identity import Identity
from packages.config.settings import SecuritySettings
from packages.domain.errors import ConfigurationError, DomainError


class InvalidToken(DomainError):
    code = "INVALID_TOKEN"
    http_status = 401
    safe_message = "The provided access token is invalid or has expired."


class _AccessTokenClaims(BaseModel):
    sub: str  # user_id
    tenant_id: str
    email: str
    roles: list[str]
    groups: list[str]
    department: str | None = None
    iss: str
    aud: str
    exp: int
    iat: int


def create_access_token(
    *,
    settings: SecuritySettings,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    email: str,
    roles: frozenset[str],
    groups: frozenset[str],
    department: str | None = None,
) -> str:
    if not settings.jwt_private_key:
        raise ConfigurationError(
            "Token issuance requires SECURITY_JWT_PRIVATE_KEY to be configured; "
            "this deployment appears to be verification-only."
        )
    now = datetime.now(UTC)
    claims = _AccessTokenClaims(
        sub=str(user_id),
        tenant_id=str(tenant_id),
        email=email,
        roles=sorted(roles),
        groups=sorted(groups),
        department=department,
        iss=settings.jwt_issuer,
        aud=settings.jwt_audience,
        iat=int(now.timestamp()),
        exp=int((now + timedelta(seconds=settings.access_token_ttl_seconds)).timestamp()),
    )
    return jwt.encode(
        claims.model_dump(), settings.jwt_private_key, algorithm=settings.jwt_algorithm
    )


def verify_access_token(token: str, *, settings: SecuritySettings) -> Identity:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.access_token_leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
        claims = _AccessTokenClaims.model_validate(payload)
    except (jwt.PyJWTError, ValidationError) as exc:
        raise InvalidToken() from exc

    try:
        user_id = uuid.UUID(claims.sub)
        tenant_id = uuid.UUID(claims.tenant_id)
    except ValueError as exc:
        raise InvalidToken() from exc

    return Identity(
        user_id=user_id,
        tenant_id=tenant_id,
        email=claims.email,
        roles=frozenset(claims.roles),
        groups=frozenset(claims.groups),
        department=claims.department,
    )
