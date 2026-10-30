from __future__ import annotations

import json
import uuid

import httpx
import pytest
from rag.reranking.base import RerankCandidate
from rag.reranking.voyage_reranker import RerankProviderUnavailable, VoyageReranker


def _make_reranker(transport: httpx.MockTransport) -> VoyageReranker:
    reranker = VoyageReranker(api_key="test-key")
    reranker._client = httpx.AsyncClient(transport=transport)
    return reranker


@pytest.mark.unit
async def test_rerank_maps_response_indices_back_to_chunk_ids() -> None:
    candidates = [
        RerankCandidate(chunk_id=uuid.uuid4(), text="first document"),
        RerankCandidate(chunk_id=uuid.uuid4(), text="second document"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )

    reranker = _make_reranker(httpx.MockTransport(handler))
    results = await reranker.rerank("query", candidates)

    assert results[0].chunk_id == candidates[1].chunk_id
    assert results[0].score == 0.9
    assert results[1].chunk_id == candidates[0].chunk_id


@pytest.mark.unit
async def test_rerank_sends_correct_request_shape() -> None:
    captured: dict[str, object] = {}
    candidates = [RerankCandidate(chunk_id=uuid.uuid4(), text="doc a")]

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"index": 0, "relevance_score": 0.5}]})

    reranker = _make_reranker(httpx.MockTransport(handler))
    await reranker.rerank("what is x", candidates)

    assert captured["payload"] == {
        "query": "what is x",
        "documents": ["doc a"],
        "model": "rerank-2",
    }
    assert captured["auth"] == "Bearer test-key"


@pytest.mark.unit
async def test_empty_candidates_returns_empty_without_calling_api() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"data": []})

    reranker = _make_reranker(httpx.MockTransport(handler))
    results = await reranker.rerank("query", [])

    assert results == []
    assert call_count == 0


@pytest.mark.unit
async def test_http_status_error_is_converted_without_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, json={"error": "internal"})

    reranker = _make_reranker(httpx.MockTransport(handler))
    with pytest.raises(RerankProviderUnavailable):
        await reranker.rerank("q", [RerankCandidate(chunk_id=uuid.uuid4(), text="x")])
    assert call_count == 1


@pytest.mark.unit
async def test_transport_error_actually_retries() -> None:
    """Regression test proving the retry-structure lesson from Phase 11 was applied
    correctly here: tenacity must genuinely observe and retry the transport error."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"data": [{"index": 0, "relevance_score": 0.7}]})

    reranker = _make_reranker(httpx.MockTransport(handler))
    results = await reranker.rerank("q", [RerankCandidate(chunk_id=uuid.uuid4(), text="x")])

    assert call_count == 3
    assert results[0].score == 0.7
