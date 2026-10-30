"""Fingerprints of key material that is committed to this repository for tests.

A private key in version control is public. These test keys must never verify tokens in a real
environment; Settings refuses them in staging/production. Fingerprints (SHA-256 of the
whitespace-stripped PEM) are stored, not the keys.
"""

from __future__ import annotations

import hashlib
import re

# .env.test public key (its private half is committed in .env.test)
_COMMITTED_TEST_PUBLIC_KEY_SHA256 = frozenset(
    {"2ca171b4b04eb65434965ca91ee9347a81ef3c228175b25e8fe56c1f3eace604"}
)


def public_key_fingerprint(pem: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", "", pem).encode()).hexdigest()


def is_committed_test_key(pem: str) -> bool:
    return public_key_fingerprint(pem) in _COMMITTED_TEST_PUBLIC_KEY_SHA256
