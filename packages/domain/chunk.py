"""Chunk value object. In the domain layer because persistence stores it and chunkers produce it."""

from __future__ import annotations

from dataclasses import dataclass

CHUNKING_VERSION = "v1"  # bump whenever the algorithm changes (Section 20, 163)


@dataclass(frozen=True)
class Chunk:
    sequence: int
    text: str
    token_count: int
    heading_path: str | None  # e.g. "Refund Policy > Eligibility > Exceptions"
    page_number: int | None
    character_start: int
    character_end: int
    chunking_version: str = CHUNKING_VERSION
