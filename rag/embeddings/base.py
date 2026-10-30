"""
Embedding provider interface (Section 23, 105).

Every embedding call in this system goes through this Protocol, never a vendor SDK
directly, so switching providers (or, as this phase documents, running against a
deterministic local provider when no live provider is reachable) means implementing
one class, not touching any calling code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embeds documents/chunks for indexing. Order-preserving: result[i]
        corresponds to texts[i]."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """
        Embeds a user query. Kept as a separate method from embed_batch (Section 23)
        because some providers use asymmetric embeddings -- a different encoding for
        "things to search for" vs. "the search query itself" -- even though this
        phase's implementations treat them identically.
        """
        ...


@dataclass(frozen=True)
class EmbeddingBatchResult:
    vectors: list[list[float]]
    input_tokens: int | None  # None = the provider did not report usage


@runtime_checkable
class MeteredEmbeddingProvider(Protocol):
    """Optional capability: report billed input tokens alongside the vectors."""

    async def embed_batch_metered(self, texts: list[str]) -> EmbeddingBatchResult: ...
