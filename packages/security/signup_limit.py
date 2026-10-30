"""Per-client-IP attempt limit for self-service signup (Redis fixed window).

Every attempt counts (not only failures): signup creates rows, so unlike login the abuse is the
*successful* path. Same Redis-outage policy as the login throttle (`fail_open`), counted in
`ekc_login_throttle_errors_total{kind="signup"}`. Not a substitute for the gateway rate limit.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from packages.domain.errors import DomainError
from packages.observability.metrics import LOGIN_THROTTLE_ERRORS, LOGIN_THROTTLED


class TooManySignups(DomainError):
    code = "SIGNUP_THROTTLED"
    http_status = 429
    safe_message = "Too many sign-up attempts from this address. Try again later."

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__()
        self.retry_after_seconds = retry_after_seconds


class SignupLimiter:
    def __init__(
        self, redis: Redis[str], *, max_per_ip: int, window_seconds: int, fail_open: bool = True
    ) -> None:
        self._redis = redis
        self._max = max_per_ip
        self._window = window_seconds
        self._fail_open = fail_open

    async def hit(self, ip: str) -> None:
        key = f"signup:attempt:ip:{ip[:64]}"
        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, self._window)
            ttl = await self._redis.ttl(key) if count > self._max else 0
        except RedisError:
            LOGIN_THROTTLE_ERRORS.labels("signup").inc()
            if not self._fail_open:
                raise TooManySignups(self._window) from None
            return
        if count > self._max:
            LOGIN_THROTTLED.labels("signup").inc()
            raise TooManySignups(ttl if ttl > 0 else self._window)
