"""
Authentication routes.

Login requires (tenant_slug, email, password) rather than email alone, since two
different tenants may share a user's email address (Section 9) -- the client is
expected to know which tenant/workspace it's signing into, the same way Slack or
similar multi-tenant products work.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from uuid import UUID

from apps.api.app.dependencies.db import get_db_session
from fastapi import APIRouter, Depends, Request
from packages.audit.service import record_audit
from packages.auth.jwt import create_access_token
from packages.database.models import Tenant, User
from packages.database.repositories.identity import (
    assign_role_to_user,
    get_group_names_for_user,
    get_or_create_role,
    get_role_names_for_user,
    get_tenant_by_slug,
    get_user_by_tenant_and_email,
)
from packages.domain.errors import DomainError
from packages.observability.metrics import AUDIT_WRITE_FAILURES
from packages.redis.client import get_redis_client
from packages.security.http import client_ip
from packages.security.login_throttle import LoginThrottle
from packages.security.passwords import hash_password, verify_password
from packages.security.signup_limit import SignupLimiter
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_MAX_BCRYPT_BYTES = 72


def _clip(password: str) -> str:
    """Longest prefix of `password` that bcrypt will accept, for the timing-equalising dummy
    check only (never used to authenticate)."""
    return password.encode("utf-8")[:_MAX_BCRYPT_BYTES].decode("utf-8", errors="ignore")


class InvalidCredentials(DomainError):
    code = "INVALID_CREDENTIALS"
    http_status = 401
    # Deliberately identical whether the tenant, email, or password was wrong --
    # distinguishing them would let an attacker enumerate valid tenant slugs / emails.
    safe_message = "Invalid tenant, email, or password."


class WeakPassword(DomainError):
    code = "WEAK_PASSWORD"
    http_status = 422
    safe_message = "That password does not meet the password policy."


class LoginRequest(BaseModel):
    tenant_slug: str
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int


async def _audit_login(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID | None,
    outcome: str,
    reason: str | None,
    request_id: str | None,
) -> None:
    """Best effort: an audit-store outage must not turn into a login outage, but it is
    counted (`ekc_audit_write_failures_total`) so it pages someone. The email/password are
    never recorded."""
    try:
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="auth.login",
            resource_type="session",
            outcome=outcome,
            request_id=request_id,
            details={"reason": reason} if reason else {},
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()
        AUDIT_WRITE_FAILURES.inc()


_dummy_hash: str | None = None


async def _burn_password_check(password: str) -> None:
    """Spend the same bcrypt time as a real verification when the tenant/user does not exist,
    so response time does not reveal whether an account exists."""
    global _dummy_hash  # noqa: PLW0603
    if _dummy_hash is None:
        _dummy_hash = await asyncio.to_thread(hash_password, secrets.token_urlsafe(16))
    await asyncio.to_thread(verify_password, password, _dummy_hash)


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> LoginResponse:
    settings = request.app.state.settings
    hard = settings.hardening
    ip = client_ip(request.scope, hard.trusted_proxy_hops)
    throttle = LoginThrottle(
        get_redis_client(settings.redis),
        max_per_account=hard.login_max_failures_per_account,
        max_per_ip=hard.login_max_failures_per_ip,
        window_seconds=hard.login_window_seconds,
        fail_open=hard.login_throttle_fail_open,
    )
    # Refuse BEFORE any lookup or hashing. Keyed on the submitted identifier whether or not
    # it exists, so throttling is not an account-enumeration oracle.
    await throttle.check(body.tenant_slug, str(body.email), ip)

    async def fail(
        tenant_id: UUID | None, user_id: UUID | None, reason: str, request_id: str | None
    ) -> InvalidCredentials:
        await throttle.record_failure(body.tenant_slug, str(body.email), ip)
        if tenant_id is not None:
            await _audit_login(session, tenant_id, user_id, "denied", reason, request_id)
        return InvalidCredentials()

    request_id = request.headers.get("x-request-id")
    tenant = await get_tenant_by_slug(session, body.tenant_slug)
    if tenant is None or tenant.status != "active":
        await _burn_password_check(_clip(body.password))
        raise await fail(None, None, "unknown_tenant", request_id)

    user = await get_user_by_tenant_and_email(session, tenant_id=tenant.id, email=body.email)
    if user is None or user.status != "active":
        await _burn_password_check(_clip(body.password))
        raise await fail(tenant.id, None, "unknown_or_inactive_user", request_id)

    # bcrypt rejects >72-byte inputs. Treat them as an ordinary bad password on EVERY path
    # (a 422 only for existing users would be an enumeration oracle).
    too_long = len(body.password.encode("utf-8")) > _MAX_BCRYPT_BYTES
    # bcrypt is ~300 ms of CPU: run it in a worker thread (it releases the GIL) so a login
    # never stalls every other request on this event loop. Measured in Phase 33.
    if too_long or not await asyncio.to_thread(
        verify_password, body.password, user.hashed_password
    ):
        if too_long:
            await _burn_password_check(_clip(body.password))
        raise await fail(tenant.id, user.id, "bad_password", request_id)

    await throttle.record_success(body.tenant_slug, str(body.email))
    await _audit_login(session, tenant.id, user.id, "success", None, request_id)

    roles = await get_role_names_for_user(session, user_id=user.id)
    groups = await get_group_names_for_user(session, user_id=user.id)

    token = create_access_token(
        settings=settings.security,
        user_id=user.id,
        tenant_id=tenant.id,
        email=user.email,
        roles=roles,
        groups=groups,
        department=user.department,
    )
    return LoginResponse(
        access_token=token,
        expires_in_seconds=settings.security.access_token_ttl_seconds,
    )


# --------------------------------------------------------------------------------------
# Self-service signup: creates a NEW organization and its first admin. It never adds a user
# to an existing tenant (that would be account takeover of someone else's workspace).
# --------------------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
MIN_PASSWORD_LENGTH = 10


class SignupDisabled(DomainError):
    code = "SIGNUP_DISABLED"
    http_status = 403
    safe_message = "Self-service sign-up is not enabled on this deployment."


class TenantSlugTaken(DomainError):
    code = "TENANT_SLUG_TAKEN"
    http_status = 409
    safe_message = "That organization identifier is already in use. Choose another."


def password_policy_problem(password: str, email: str) -> str | None:
    """None if acceptable. Mirrored in the web UI (apps/web/src/lib/password.ts); the server is
    the authority. bcrypt's 72-byte limit is enforced here rather than truncating."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if len(password.encode("utf-8")) > _MAX_BCRYPT_BYTES:
        return f"Use at most {_MAX_BCRYPT_BYTES} bytes."
    if not (any(c.isalpha() for c in password) and any(c.isdigit() for c in password)):
        return "Include at least one letter and one digit."
    local = email.split("@", 1)[0].lower()
    if len(local) >= 4 and local in password.lower():
        return "Do not include the part of your email before the @."
    return None


class SignupRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=120)
    tenant_slug: str = Field(min_length=3, max_length=63)
    display_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(max_length=200)

    @field_validator("organization_name", "display_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("tenant_slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SLUG_RE.fullmatch(v):
            raise ValueError(
                "use 3-63 lowercase letters, digits or hyphens, not starting/ending with a hyphen"
            )
        return v


class SignupResponse(LoginResponse):
    tenant_slug: str


@router.post("/signup", response_model=SignupResponse, status_code=201)
async def signup(
    body: SignupRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SignupResponse:
    settings = request.app.state.settings
    if not settings.signup_enabled:
        raise SignupDisabled()
    hard = settings.hardening
    ip = client_ip(request.scope, hard.trusted_proxy_hops)
    await SignupLimiter(
        get_redis_client(settings.redis),
        max_per_ip=settings.signup.max_per_ip,
        window_seconds=settings.signup.window_seconds,
        fail_open=hard.login_throttle_fail_open,
    ).hit(ip)

    problem = password_policy_problem(body.password, str(body.email))
    if problem is not None:
        raise WeakPassword(problem)

    if await get_tenant_by_slug(session, body.tenant_slug) is not None:
        raise TenantSlugTaken()
    hashed = await asyncio.to_thread(hash_password, body.password)
    try:
        tenant = Tenant(name=body.organization_name, slug=body.tenant_slug)
        session.add(tenant)
        await session.flush()
        user = User(
            tenant_id=tenant.id,
            email=str(body.email),
            hashed_password=hashed,
            display_name=body.display_name,
        )
        session.add(user)
        await session.flush()
        role = await get_or_create_role(session, tenant_id=tenant.id, name="admin")
        await assign_role_to_user(session, tenant_id=tenant.id, user_id=user.id, role_id=role.id)
        await record_audit(
            session,
            tenant_id=tenant.id,
            actor_user_id=user.id,
            action="auth.signup",
            resource_type="tenant",
            resource_id=str(tenant.id),
            outcome="success",
            request_id=request.headers.get("x-request-id"),
            details={},
        )
        await session.commit()
    except IntegrityError:
        # Lost a race on the unique slug between the check above and the insert.
        await session.rollback()
        raise TenantSlugTaken() from None

    token = create_access_token(
        settings=settings.security,
        user_id=user.id,
        tenant_id=tenant.id,
        email=user.email,
        roles=frozenset({"admin"}),
        groups=frozenset(),
        department=None,
    )
    return SignupResponse(
        access_token=token,
        expires_in_seconds=settings.security.access_token_ttl_seconds,
        tenant_slug=tenant.slug,
    )
