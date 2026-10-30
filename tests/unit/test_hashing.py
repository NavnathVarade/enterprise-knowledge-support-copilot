from __future__ import annotations

import pytest
from packages.domain.hashing import sha256_hex, sha256_hex_of_text


@pytest.mark.unit
def test_same_content_hashes_identically() -> None:
    assert sha256_hex(b"hello world") == sha256_hex(b"hello world")


@pytest.mark.unit
def test_different_content_hashes_differently() -> None:
    assert sha256_hex(b"hello world") != sha256_hex(b"hello there")


@pytest.mark.unit
def test_hash_is_64_char_hex() -> None:
    digest = sha256_hex(b"anything")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


@pytest.mark.unit
def test_text_and_bytes_helpers_agree() -> None:
    assert sha256_hex_of_text("hello") == sha256_hex(b"hello")
