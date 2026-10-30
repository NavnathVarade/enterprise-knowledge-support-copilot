"""
Content hashing (Section 19).

A single, shared implementation so "what counts as unchanged" is defined once. Used to
decide whether a re-uploaded or re-fetched document actually differs from what's already
indexed (skip reprocessing if the hash matches) and, later, for duplicate detection
across documents within a tenant.
"""

from __future__ import annotations

import hashlib


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_hex_of_text(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))
