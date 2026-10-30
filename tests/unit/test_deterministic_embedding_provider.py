from __future__ import annotations

import math

import pytest
from rag.embeddings.batching import batched
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider


@pytest.mark.unit
async def test_same_text_produces_identical_vector() -> None:
    provider = DeterministicLocalEmbeddingProvider(dimensions=64)
    v1 = await provider.embed_query("hello world")
    v2 = await provider.embed_query("hello world")
    assert v1 == v2


@pytest.mark.unit
async def test_different_text_produces_different_vectors() -> None:
    provider = DeterministicLocalEmbeddingProvider(dimensions=64)
    v1 = await provider.embed_query("hello world")
    v2 = await provider.embed_query("goodbye world")
    assert v1 != v2


@pytest.mark.unit
async def test_vector_has_configured_dimensions() -> None:
    provider = DeterministicLocalEmbeddingProvider(dimensions=128)
    vector = await provider.embed_query("test")
    assert len(vector) == 128


@pytest.mark.unit
async def test_vector_is_unit_normalized() -> None:
    provider = DeterministicLocalEmbeddingProvider(dimensions=64)
    vector = await provider.embed_query("normalize me")
    norm = math.sqrt(sum(v * v for v in vector))
    assert norm == pytest.approx(1.0, abs=1e-6)


@pytest.mark.unit
async def test_embed_batch_preserves_order_and_matches_embed_query() -> None:
    provider = DeterministicLocalEmbeddingProvider(dimensions=32)
    texts = ["alpha", "beta", "gamma"]
    batch_result = await provider.embed_batch(texts)
    individual_results = [await provider.embed_query(t) for t in texts]
    assert batch_result == individual_results


@pytest.mark.unit
def test_batched_splits_evenly() -> None:
    items = ["a", "b", "c", "d"]
    assert batched(items, 2) == [["a", "b"], ["c", "d"]]


@pytest.mark.unit
def test_batched_handles_remainder() -> None:
    items = ["a", "b", "c", "d", "e"]
    assert batched(items, 2) == [["a", "b"], ["c", "d"], ["e"]]


@pytest.mark.unit
def test_batched_single_batch_when_size_exceeds_list() -> None:
    assert batched(["a", "b"], 10) == [["a", "b"]]


@pytest.mark.unit
def test_batched_rejects_non_positive_batch_size() -> None:
    with pytest.raises(ValueError):
        batched(["a"], 0)


@pytest.mark.unit
def test_batched_empty_list_returns_empty() -> None:
    assert batched([], 5) == []
