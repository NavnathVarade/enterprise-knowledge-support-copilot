from __future__ import annotations

import json

import httpx
import pytest
from packages.llm.anthropic_provider import AnthropicProvider
from packages.llm.base import (
    LLMMessage,
    LLMRequest,
    ProviderAuthError,
    ProviderInvalidRequest,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderTransientError,
)

pytestmark = pytest.mark.unit

REQ = LLMRequest(system="S", messages=[LLMMessage("user", "Q")], max_output_tokens=50)


def _provider(handler: httpx.MockTransport) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="k", model="m1", timeout_seconds=5, client=httpx.AsyncClient(transport=handler)
    )


async def test_request_shape_and_response_parsing() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "m1",
                "content": [{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}],
                "usage": {"input_tokens": 7, "output_tokens": 3},
                "stop_reason": "end_turn",
            },
        )

    r = await _provider(httpx.MockTransport(handler)).complete(REQ)
    assert r.text == "Hello" and r.input_tokens == 7 and r.output_tokens == 3
    assert seen["headers"]["x-api-key"] == "k"  # type: ignore[index]
    assert seen["headers"]["anthropic-version"] == "2023-06-01"  # type: ignore[index]
    body = seen["body"]
    assert body["system"] == "S" and body["max_tokens"] == 50  # type: ignore[index]
    assert body["messages"] == [{"role": "user", "content": "Q"}]  # type: ignore[index]


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (429, ProviderRateLimited),
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (400, ProviderInvalidRequest),
        (413, ProviderInvalidRequest),
        (500, ProviderTransientError),
        (529, ProviderTransientError),
        (302, ProviderInvalidResponse),
    ],
)
async def test_status_mapping(status: int, exc: type[Exception]) -> None:
    p = _provider(httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(exc):
        await p.complete(REQ)


async def test_retry_after_header_parsed() -> None:
    p = _provider(httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "7"})))
    with pytest.raises(ProviderRateLimited) as ei:
        await p.complete(REQ)
    assert ei.value.retry_after_seconds == 7.0


async def test_timeout_and_transport_errors() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("t")

    def conn(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("c")

    with pytest.raises(ProviderTimeout):
        await _provider(httpx.MockTransport(timeout)).complete(REQ)
    with pytest.raises(ProviderTransientError):
        await _provider(httpx.MockTransport(conn)).complete(REQ)


async def test_malformed_200_body() -> None:
    p = _provider(httpx.MockTransport(lambda r: httpx.Response(200, json={"oops": 1})))
    with pytest.raises(ProviderInvalidResponse):
        await p.complete(REQ)
