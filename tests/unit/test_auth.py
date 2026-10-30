from __future__ import annotations

import time
import uuid

import jwt as pyjwt
import pytest
from packages.auth.jwt import InvalidToken, create_access_token, verify_access_token
from packages.config.settings import SecuritySettings
from packages.security.passwords import hash_password, verify_password


@pytest.mark.unit
def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password", hashed)


@pytest.mark.unit
def test_password_hash_is_not_plaintext() -> None:
    hashed = hash_password("my-secret-password")
    assert "my-secret-password" not in hashed
    assert hashed.startswith("$2b$")  # bcrypt marker


@pytest.mark.unit
def test_overlong_password_is_rejected_not_silently_truncated() -> None:
    from packages.security.passwords import PasswordTooLong

    too_long = "a" * 73  # bcrypt's hard limit is 72 bytes
    with pytest.raises(PasswordTooLong):
        hash_password(too_long)


@pytest.fixture
def security_settings() -> SecuritySettings:
    from tests.conftest import TEST_JWT_PRIVATE_KEY, TEST_JWT_PUBLIC_KEY

    return SecuritySettings(
        jwt_public_key=TEST_JWT_PUBLIC_KEY,
        jwt_private_key=TEST_JWT_PRIVATE_KEY,
        jwt_issuer="ekc-test",
        jwt_audience="ekc-test",
        access_token_ttl_seconds=900,
    )


@pytest.mark.unit
def test_access_token_roundtrip(security_settings: SecuritySettings) -> None:
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    token = create_access_token(
        settings=security_settings,
        user_id=user_id,
        tenant_id=tenant_id,
        email="alice@example.com",
        roles=frozenset({"admin"}),
        groups=frozenset({"engineering"}),
    )
    identity = verify_access_token(token, settings=security_settings)

    assert identity.user_id == user_id
    assert identity.tenant_id == tenant_id
    assert identity.email == "alice@example.com"
    assert identity.has_role("admin")
    assert "engineering" in identity.groups


@pytest.mark.unit
def test_expired_token_is_rejected(security_settings: SecuritySettings) -> None:
    short_lived = security_settings.model_copy(update={"access_token_ttl_seconds": 1})
    token = create_access_token(
        settings=short_lived,
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="bob@example.com",
        roles=frozenset(),
        groups=frozenset(),
    )
    # exp/iat are int()-truncated Unix timestamps (see packages/auth/jwt.py), which can
    # lose up to ~1 second of precision depending on where `now` falls within its
    # fractional second. A tight sleep margin against a 1-second TTL is therefore a
    # race; sleeping well past the worst case makes this deterministic.
    time.sleep(2.5)
    with pytest.raises(InvalidToken):
        verify_access_token(
            token, settings=short_lived.model_copy(update={"access_token_leeway_seconds": 0})
        )


@pytest.mark.unit
def test_token_with_wrong_audience_is_rejected(security_settings: SecuritySettings) -> None:
    token = create_access_token(
        settings=security_settings,
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="carol@example.com",
        roles=frozenset(),
        groups=frozenset(),
    )
    wrong_audience_settings = security_settings.model_copy(
        update={"jwt_audience": "some-other-api"}
    )
    with pytest.raises(InvalidToken):
        verify_access_token(token, settings=wrong_audience_settings)


@pytest.mark.unit
def test_tampered_token_is_rejected(security_settings: SecuritySettings) -> None:
    token = create_access_token(
        settings=security_settings,
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        email="dave@example.com",
        roles=frozenset(),
        groups=frozenset({"finance"}),
    )
    # Decode without verification, escalate privileges, re-encode with a DIFFERENT
    # (attacker-controlled) key -- verification against the real public key must fail.
    unverified_claims = pyjwt.decode(token, options={"verify_signature": False})
    unverified_claims["roles"] = ["admin"]
    forged_token = pyjwt.encode(
        unverified_claims, "attacker-controlled-secret-0123456789abcdef", algorithm="HS256"
    )

    with pytest.raises(InvalidToken):
        verify_access_token(forged_token, settings=security_settings)
