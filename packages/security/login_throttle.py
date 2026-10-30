"""Login brute-force protection (Redis fixed-window failure counters).

Two independent counters, both checked BEFORE any password work:
  * per ACCOUNT  -- (tenant slug, email), whether or not that account exists, so the
    throttle is not an account-enumeration oracle;
  * per CLIENT IP -- catches one source spraying many accounts.
Only failures count; a successful login clears the account counter (not the IP's).

Known trade-offs (documented in docs/security/PHASE_32_SECURITY_HARDENING.md):
  * a known account can be locked for one window by someone who fails against it
    (availability for security; the IP counter does not help against distributed attempts);
  * fixed windows allow up to 2x the limit across a boundary;
  * Redis is not the source of truth; on a Redis error the throttle follows
    `login_throttle_fail_open` (default: allow) and `ekc_login_throttle_errors_total` counts it.
"""

from __future__ import annotations

import hashlib

from redis.asyncio import Redis
from redis.exceptions import RedisError

from packages.domain.errors import DomainError
from packages.observability.logging import get_logger
from packages.observability.metrics import LOGIN_THROTTLE_ERRORS, LOGIN_THROTTLED
from packages.redis.breaker import CooldownBreaker

_log = get_logger(component="login_throttle")

# Shared by every request in this process (LoginThrottle itself is created per request).
LOGIN_BREAKER = CooldownBreaker(cooldown_seconds=10.0)


class TooManyLoginAttempts(DomainError):
    code = "LOGIN_THROTTLED"
    http_status = 429
    safe_message = "Too many failed sign-in attempts. Try again later."


def account_key(tenant_slug: str, email: str) -> str:
    """Hashed so Redis never holds email addresses."""
    ident = f"{tenant_slug.strip().lower()}|{email.strip().lower()}"
    return f"login:fail:acct:{hashlib.sha256(ident.encode()).hexdigest()[:32]}"


def ip_key(ip: str) -> str:
    return f"login:fail:ip:{ip[:64]}"


class LoginThrottle:
    def __init__(
        self,
        redis: Redis[str],
        *,
        max_per_account: int,
        max_per_ip: int,
        window_seconds: int,
        fail_open: bool = True,
        breaker: CooldownBreaker | None = None,
    ) -> None:
        self._breaker = breaker if breaker is not None else LOGIN_BREAKER
        self._redis = redis
        self._max_acct = max_per_account
        self._max_ip = max_per_ip
        self._window = window_seconds
        self._fail_open = fail_open

    async def check(self, tenant_slug: str, email: str, ip: str) -> None:
        if self._breaker.is_open:
            self._skip("check")
            return
        try:
            acct, per_ip = await self._redis.mget([account_key(tenant_slug, email), ip_key(ip)])
        except RedisError:
            self._on_error("check")
            return
        self._breaker.reset()
        if acct is not None and int(acct) >= self._max_acct:
            LOGIN_THROTTLED.labels("account").inc()
            raise TooManyLoginAttempts()
        if per_ip is not None and int(per_ip) >= self._max_ip:
            LOGIN_THROTTLED.labels("ip").inc()
            raise TooManyLoginAttempts()

    async def record_failure(self, tenant_slug: str, email: str, ip: str) -> None:
        if self._breaker.is_open:
            self._skip("record")
            return
        try:
            for key in (account_key(tenant_slug, email), ip_key(ip)):
                if await self._redis.incr(key) == 1:
                    await self._redis.expire(key, self._window)
        except RedisError:
            self._on_error("record")

    async def record_success(self, tenant_slug: str, email: str) -> None:
        if self._breaker.is_open:
            self._skip("reset")
            return
        try:
            await self._redis.delete(account_key(tenant_slug, email))
        except RedisError:
            self._on_error("reset")

    def _skip(self, op: str) -> None:
        """Breaker open: do not touch Redis; apply the configured policy immediately."""
        LOGIN_THROTTLE_ERRORS.labels(op).inc()
        if not self._fail_open:
            raise TooManyLoginAttempts()

    def _on_error(self, op: str) -> None:
        LOGIN_THROTTLE_ERRORS.labels(op).inc()
        if self._breaker.trip():  # log the transition once, not every request
            _log.error("login_throttle_redis_error", op=op, fail_open=self._fail_open)
        if not self._fail_open:
            raise TooManyLoginAttempts()
