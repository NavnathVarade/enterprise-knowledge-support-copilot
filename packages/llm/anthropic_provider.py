"""
Anthropic Messages API provider -- a real HTTP client.

NOT NETWORK-VERIFIED: api.anthropic.com is not reachable from the build sandbox. Request
and response shapes follow the documented Messages API and are exercised against a mock
transport only. Retries are NOT done here; the gateway owns retry/fallback so there is a
single place that bounds total attempts.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from packages.llm.base import (
    LLMRequest,
    LLMResponse,
    ProviderAuthError,
    ProviderInvalidRequest,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderTransientError,
)

_URL = "https://api.anthropic.com/v1/messages"
_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        started = time.perf_counter()
        try:
            response = await self._client.post(
                _URL,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": _VERSION,
                    "content-type": "application/json",
                },
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout("anthropic request timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                f"anthropic transport error: {type(exc).__name__}"
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000
        status = response.status_code
        if status == 429:
            raise ProviderRateLimited("anthropic rate limited", _retry_after(response))
        if status in (401, 403):
            raise ProviderAuthError(f"anthropic auth failure ({status})")
        if status in (400, 404, 413, 422):
            raise ProviderInvalidRequest(f"anthropic rejected request ({status})")
        if status >= 500:  # includes 529 overloaded
            raise ProviderTransientError(f"anthropic server error ({status})")
        if status != 200:
            raise ProviderInvalidResponse(f"unexpected status {status}")

        try:
            data = response.json()
            text = "".join(
                block["text"] for block in data["content"] if block.get("type") == "text"
            )
            usage = data["usage"]
            return LLMResponse(
                text=text,
                provider=self.name,
                model=str(data.get("model", self.model)),
                input_tokens=int(usage["input_tokens"]),
                output_tokens=int(usage["output_tokens"]),
                stop_reason=data.get("stop_reason"),
                latency_ms=latency_ms,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderInvalidResponse("anthropic response missing expected fields") from exc


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
