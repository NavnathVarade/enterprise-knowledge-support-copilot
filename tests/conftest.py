"""
Shared pytest configuration and fixtures.

TEST_JWT_PRIVATE_KEY / TEST_JWT_PUBLIC_KEY are generated fresh here rather than read
from .env.test, so `tests/unit/test_auth.py` (pure unit tests with no database/HTTP
involved) can run standalone without the full application environment being sourced --
matching the unit/integration split described in pyproject.toml's pytest markers.
"""

from __future__ import annotations

import os

# A developer's own `.env` must never leak into the test run (explicit variables still win).
os.environ.setdefault("EKC_ENV_FILE", "")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

TEST_JWT_PRIVATE_KEY: str = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

TEST_JWT_PUBLIC_KEY: str = (
    _key.public_key()
    .public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode()
)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_redis_breakers() -> None:
    """Process-wide Redis breakers must not leak an open state from one test into the next."""
    from packages.llm.rate_limit import RATE_LIMIT_BREAKER
    from packages.security.login_throttle import LOGIN_BREAKER

    LOGIN_BREAKER.reset()
    RATE_LIMIT_BREAKER.reset()
