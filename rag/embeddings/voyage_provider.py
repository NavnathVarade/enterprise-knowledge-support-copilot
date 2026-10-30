"""
Voyage AI embedding provider -- a real HTTP client, not a stub.

NOT NETWORK-TESTED in this environment: api.voyageai.com is not reachable from this
sandbox's network allowlist (see docs/rag/PHASE_11_EMBEDDING_DESIGN.md for the full
explanation, matching the Kafka broker gap from Phase 8). This class is written the way
it would actually be called in production -- real request/response shapes per Voyage's
documented API, real batching against EmbeddingSettings.batch_size, real bounded retry
with backoff via tenacity, real timeout -- and unit-tested with a mocked HTTP transport
so its request-construction and error-handling logic is verified even though the live
call is not.
"""

from __future__ import annotations

import httpx
from packages.config.settings import EmbeddingSettings
from packages.domain.errors import EmbeddingProviderUnavailable
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from rag.embeddings.base import EmbeddingBatchResult
from rag.embeddings.batching import batched

_VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"


class VoyageEmbeddingProvider:
    def __init__(self, *, api_key: str, settings: EmbeddingSettings) -> None:
        self._api_key = api_key
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    @property
    def model_name(self) -> str:
        return self._settings.model

    @property
    def dimensions(self) -> int:
        return self._settings.dimensions

    @retry(
        # IMPORTANT: this function must let httpx.TransportError propagate
        # UNCONVERTED, so tenacity's own exception-type check here actually sees it.
        # Converting it to a domain error inside the function body (as an earlier
        # version of this code did) means tenacity never observes a TransportError at
        # all -- it only ever sees whatever the function raises AFTER conversion,
        # which never matches retry_if_exception_type, so retries silently never fire.
        # HTTPStatusError (a 4xx/5xx response) is deliberately NOT retried -- most
        # status errors (auth failure, bad request) won't be fixed by retrying, and
        # `stop_after_attempt` bounds the transport-error case anyway.
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _call_api_raw(
        self, texts: list[str], input_type: str
    ) -> tuple[list[list[float]], int | None]:
        response = await self._client.post(
            _VOYAGE_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"input": texts, "model": self._settings.model, "input_type": input_type},
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return [item["embedding"] for item in data["data"]], (
            tokens if isinstance(tokens, int) and tokens >= 0 else None
        )

    async def _call_api(
        self, texts: list[str], input_type: str
    ) -> tuple[list[list[float]], int | None]:
        """
        Public-facing wrapper: converts to the domain error AFTER retries (via
        `_call_api_raw`) are exhausted, so callers only ever see
        `EmbeddingProviderUnavailable`, never a raw httpx exception -- while retries
        still actually function because `_call_api_raw` itself let the original
        exception type through to tenacity.
        """
        try:
            return await self._call_api_raw(texts, input_type)
        except httpx.HTTPStatusError as exc:
            raise EmbeddingProviderUnavailable(
                f"Voyage API returned {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.TransportError as exc:
            raise EmbeddingProviderUnavailable(
                f"Voyage API unreachable after retries: {exc}"
            ) from exc

    async def embed_batch_metered(self, texts: list[str]) -> EmbeddingBatchResult:
        """Vectors plus the provider-reported token count (None if any call omitted usage)."""
        results: list[list[float]] = []
        total: int | None = 0
        for batch in batched(texts, self._settings.batch_size):
            vectors, tokens = await self._call_api(batch, input_type="document")
            results.extend(vectors)
            total = None if (tokens is None or total is None) else total + tokens
        return EmbeddingBatchResult(results, total)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return (await self.embed_batch_metered(texts)).vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors, _ = await self._call_api([text], input_type="query")
        return vectors[0]

    async def aclose(self) -> None:
        await self._client.aclose()
