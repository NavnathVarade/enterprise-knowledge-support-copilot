"""
Password hashing.

Uses the `bcrypt` library directly rather than via passlib. passlib is unmaintained
(no release since 2020) and its bcrypt backend's version-detection shim is broken
against bcrypt>=4.1's API changes -- this surfaced as a real, reproducible test failure
("password cannot be longer than 72 bytes") even for short passwords, traced to
passlib's `_load_backend_mixin` misreading the installed bcrypt version. Calling
`bcrypt` directly avoids the broken compatibility layer entirely.

bcrypt has a hard 72-byte input limit (its own primitive, not a passlib artifact) --
this module explicitly rejects passwords over that limit up front, at hashing AND
verification time, rather than silently truncating (silent truncation is a real,
historically-exploited bcrypt footgun: "myextremelylongpassword...suffix1" and
"...suffix2" would hash identically).
"""

from __future__ import annotations

import bcrypt

from packages.domain.errors import DomainError

_MAX_PASSWORD_BYTES = 72


class PasswordTooLong(DomainError):
    code = "PASSWORD_TOO_LONG"
    http_status = 422
    safe_message = "Password exceeds the maximum allowed length."


def _check_length(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise PasswordTooLong()
    return encoded


def hash_password(plain_password: str) -> str:
    encoded = _check_length(plain_password)
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    encoded = _check_length(plain_password)
    return bcrypt.checkpw(encoded, hashed_password.encode("utf-8"))
