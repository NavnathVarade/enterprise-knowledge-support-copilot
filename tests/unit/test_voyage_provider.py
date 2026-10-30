from __future__ import annotations

import json

import httpx
import pytest
from packages.config.settings import EmbeddingSettings
from packages.domain.errors import EmbeddingProviderUnavailable
from rag.embeddings.voyage_provider import VoyageEmbeddingProvider


def _settings(**overrides: object) -> EmbeddingSettings:
    defaults: dict[str, object] = {
        "provider": "voyage",
        "model": "voyage-3-large",
        "dimensions": 4,
        "batch_size": 2,
        "request_timeout_seconds": 5.0,
    }
    defaults.update(overrides)
    return EmbeddingSettings(**defaults)  # type: ignore[arg-type]


def _make_provider(
    transport: httpx.MockTransport, **settings_overrides: object
) -> VoyageEmbeddingProvider:
    provider = VoyageEmbeddingProvider(api_key="test-key", settings=_settings(**settings_overrides))
    provider._client = httpx.AsyncClient(transport=transport)  # inject the fake transport
    return provider


@pytest.mark.unit
async def test_embed_batch_sends_correct_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        payload = json.loads(request.content)
        captured["payload"] = payload
        return httpx.Response(
            200, json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]} for _ in payload["input"]]}
        )

    transport = httpx.MockTransport(handler)
    provider = _make_provider(transport)

    result = await provider.embed_batch(["hello world"])

    assert captured["url"] == "https://api.voyageai.com/v1/embeddings"
    assert captured["auth"] == "Bearer test-key"
    assert captured["payload"] == {
        "input": ["hello world"],
        "model": "voyage-3-large",
        "input_type": "document",
    }
    assert result == [[0.1, 0.2, 0.3, 0.4]]


@pytest.mark.unit
async def test_embed_query_uses_query_input_type() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]})

    provider = _make_provider(httpx.MockTransport(handler))
    result = await provider.embed_query("what is the refund policy")

    assert captured["payload"]["input_type"] == "query"  # type: ignore[index]
    assert result == [1.0, 0.0, 0.0, 0.0]


@pytest.mark.unit
async def test_embed_batch_respects_batch_size() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        payload = json.loads(request.content)
        return httpx.Response(
            200, json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]} for _ in payload["input"]]}
        )

    provider = _make_provider(httpx.MockTransport(handler), batch_size=2)
    texts = ["a", "b", "c", "d", "e"]  # 5 texts, batch_size=2 -> 3 API calls
    result = await provider.embed_batch(texts)

    assert call_count == 3
    assert len(result) == 5


@pytest.mark.unit
async def test_http_status_error_is_converted_to_domain_error_without_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, json={"error": "invalid api key"})

    provider = _make_provider(httpx.MockTransport(handler))

    with pytest.raises(EmbeddingProviderUnavailable):
        await provider.embed_query("test")

    # A 401 is not a transient transport error -- must NOT be retried.
    assert call_count == 1


@pytest.mark.unit
async def test_transport_error_actually_retries_and_eventually_succeeds() -> None:
    """
    THE regression test for the bug this phase found: tenacity must actually see and
    retry on httpx.TransportError, not just on the first attempt.
    """
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.5, 0.5, 0.5]}]})

    provider = _make_provider(httpx.MockTransport(handler))
    result = await provider.embed_query("retry me")

    assert call_count == 3  # failed twice, succeeded on the 3rd attempt
    assert result == [0.5, 0.5, 0.5, 0.5]


@pytest.mark.unit
async def test_transport_error_exhausts_retries_and_raises_domain_error() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("always down", request=request)

    provider = _make_provider(httpx.MockTransport(handler))

    with pytest.raises(EmbeddingProviderUnavailable):
        await provider.embed_query("never works")

    assert call_count == 3  # stop_after_attempt(3)


def _usage_handler(usages: list[object]) -> httpx.MockTransport:
    calls = iter(usages)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        body: dict[str, object] = {
            "data": [{"embedding": [0.1, 0.2, 0.3, 0.4]} for _ in payload["input"]]
        }
        usage = next(calls)
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


@pytest.mark.unit
async def test_metered_batch_sums_reported_tokens_across_batches() -> None:
    provider = _make_provider(_usage_handler([{"total_tokens": 7}, {"total_tokens": 5}]))
    result = await provider.embed_batch_metered(["a", "b", "c"])  # batch_size=2 -> 2 calls
    assert len(result.vectors) == 3 and result.input_tokens == 12


@pytest.mark.unit
async def test_metered_batch_reports_none_if_any_call_omits_usage() -> None:
    provider = _make_provider(_usage_handler([{"total_tokens": 7}, None]))
    result = await provider.embed_batch_metered(["a", "b", "c"])
    assert result.input_tokens is None  # never a partial sum presented as the total


@pytest.mark.unit
async def test_malformed_usage_is_treated_as_unreported() -> None:
    for bad in ({"total_tokens": -3}, {"total_tokens": "12"}, "nope"):
        provider = _make_provider(_usage_handler([bad]))
        assert (await provider.embed_batch_metered(["a"])).input_tokens is None


@pytest.mark.unit
async def test_plain_embed_batch_still_returns_vectors_only() -> None:
    provider = _make_provider(_usage_handler([{"total_tokens": 3}]))
    vectors = await provider.embed_batch(["a"])
    assert vectors == [[0.1, 0.2, 0.3, 0.4]]
