"""
Voyage AI rerank provider -- a real HTTP client, not a stub.

NOT NETWORK-TESTED in this environment, for the same reason as
rag/embeddings/voyage_provider.py: api.voyageai.com is outside this sandbox's network
allowlist. Request/response shapes match Voyage's documented rerank API; unit-tested
with a mocked HTTP transport.

RETRY STRUCTURE NOTE: Phase 11's VoyageEmbeddingProvider originally had a bug where
converting httpx.TransportError to a domain error INSIDE the @retry-decorated function
meant tenacity never observed the exception type it was configured to retry on, so
retries silently never fired. This module applies that lesson from the start: the
decorated function (`_call_api_raw`) lets the raw exception propagate; only the
public-facing wrapper (`_call_api`) converts it, after retries are exhausted.
"""

from __future__ import annotations

import httpx
from packages.domain.errors import DomainError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from rag.reranking.base import RerankCandidate, RerankResult

_VOYAGE_RERANK_API_URL = "https://api.voyageai.com/v1/rerank"


class RerankProviderUnavailable(DomainError):
    code = "RERANK_PROVIDER_UNAVAILABLE"
    http_status = 503
    safe_message = "Reranking is temporarily unavailable."


class VoyageReranker:
    def __init__(
        self, *, api_key: str, model: str = "rerank-2", timeout_seconds: float = 20.0
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _call_api_raw(self, query: str, documents: list[str]) -> list[dict[str, object]]:
        response = await self._client.post(
            _VOYAGE_RERANK_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"query": query, "documents": documents, "model": self._model},
        )
        response.raise_for_status()
        data: dict[str, object] = response.json()
        results = data.get("data")
        if not isinstance(results, list):
            raise RerankProviderUnavailable("Voyage rerank API returned an unexpected payload.")
        return results

    async def _call_api(self, query: str, documents: list[str]) -> list[dict[str, object]]:
        try:
            return await self._call_api_raw(query, documents)
        except httpx.HTTPStatusError as exc:
            raise RerankProviderUnavailable(
                f"Voyage rerank API returned {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.TransportError as exc:
            raise RerankProviderUnavailable(
                f"Voyage rerank API unreachable after retries: {exc}"
            ) from exc

    async def rerank(self, query: str, candidates: list[RerankCandidate]) -> list[RerankResult]:
        if not candidates:
            return []
        documents = [c.text for c in candidates]
        raw_results = await self._call_api(query, documents)

        results: list[RerankResult] = []
        for item in raw_results:
            index = item["index"]
            score = item["relevance_score"]
            if not isinstance(index, int) or not 0 <= index < len(candidates):
                raise RerankProviderUnavailable("Voyage rerank API returned an invalid index.")
            if isinstance(score, bool) or not isinstance(score, int | float):
                raise RerankProviderUnavailable("Voyage rerank API returned an invalid score.")
            results.append(RerankResult(chunk_id=candidates[index].chunk_id, score=float(score)))
        return sorted(results, key=lambda r: -r.score)

    async def aclose(self) -> None:
        await self._client.aclose()
