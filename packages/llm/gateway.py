"""
LLM gateway (Section 37-38): the only entry point the application uses to call a model.

Behavior, in order:
  1. Rate limit (per tenant).
  2. Pre-flight context check against max_context_tokens (heuristic estimate) -> never
     send a request that is known to overflow.
  3. Primary provider with bounded retries: exponential backoff + full jitter, honoring
     a provider Retry-After (capped), per-attempt timeout. Only transient errors retry.
  4. If the primary is exhausted (transient) or failed auth, try the fallback provider
     once through the same bounded retry path. InvalidRequest never falls back.
  5. Otherwise raise a controlled domain error. NEVER fabricate an answer.

Every success returns an `LLMResult` with token usage, cost (None if no price is
configured), attempts and whether the fallback served it.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from packages.domain.errors import (
    LLMContextOverflow,
    LLMInvalidResponse,
    LLMTimeout,
    LLMUnavailable,
)
from packages.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderAuthError,
    ProviderError,
    ProviderInvalidRequest,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderTransientError,
)
from packages.llm.pricing import ModelPricing, compute_cost_usd
from packages.llm.rate_limit import NoopRateLimiter, RateLimiter
from packages.observability.logging import get_logger
from packages.observability.metrics import (
    LLM_COST,
    LLM_DURATION,
    LLM_REQUESTS,
    LLM_RETRIES,
    LLM_TOKENS,
)
from packages.observability.tracing import safe_attributes, span
from packages.usage.budget import BudgetGuard, NoopBudgetGuard
from packages.usage.recorder import NoopUsageRecorder, UsageRecord, UsageRecorder

_log = get_logger(component="llm_gateway")


def estimate_tokens(text: str) -> int:
    """~4 chars/token heuristic (same as Phase 10). NOT an exact tokenizer."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class LLMResult:
    response: LLMResponse
    cost_usd: float | None
    attempts: int
    fallback_used: bool


class LLMGateway:
    def __init__(
        self,
        primary: LLMProvider,
        *,
        fallback: LLMProvider | None = None,
        max_retries: int = 2,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 8.0,
        attempt_timeout_seconds: float = 30.0,
        max_context_tokens: int = 12_000,
        pricing: dict[str, ModelPricing] | None = None,
        rate_limiter: RateLimiter | None = None,
        budget_guard: BudgetGuard | None = None,
        usage_recorder: UsageRecorder | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._max_retries = max_retries
        self._base = base_delay_seconds
        self._cap = max_delay_seconds
        self._timeout = attempt_timeout_seconds
        self._max_context = max_context_tokens
        self._pricing = pricing or {}
        self._limiter = rate_limiter or NoopRateLimiter()
        self._budget = budget_guard or NoopBudgetGuard()
        self._recorder = usage_recorder or NoopUsageRecorder()
        self._sleep = sleep
        # Retry jitter, not a security primitive.
        self._rng = rng or random.Random()  # nosec B311

    async def complete(self, request: LLMRequest, *, tenant_id: uuid.UUID) -> LLMResult:
        """Instrumented entry point: one metric sample and one span per logical call
        (retries and fallback happen inside and are counted separately)."""
        start = time.perf_counter()
        provider, model = self._primary.name, self._primary.model
        with span("llm.complete", provider=provider, model=model) as sp:
            try:
                result = await self._complete(request, tenant_id=tenant_id)
            except Exception as exc:
                LLM_REQUESTS.labels(
                    provider, model, _OUTCOMES.get(type(exc).__name__, "error")
                ).inc()
                LLM_DURATION.labels(provider, model).observe(time.perf_counter() - start)
                raise
            LLM_DURATION.labels(provider, model).observe(time.perf_counter() - start)
            used_provider, used_model = result.response.provider, result.response.model
            outcome = "fallback" if result.fallback_used else "success"
            LLM_REQUESTS.labels(used_provider, used_model, outcome).inc()
            LLM_TOKENS.labels(used_provider, used_model, "input").inc(result.response.input_tokens)
            LLM_TOKENS.labels(used_provider, used_model, "output").inc(
                result.response.output_tokens
            )
            if result.cost_usd is not None:
                LLM_COST.labels(used_provider, used_model).inc(result.cost_usd)
            await self._recorder.record(
                UsageRecord(
                    tenant_id=tenant_id,
                    kind="llm",
                    provider=used_provider,
                    model=used_model,
                    input_tokens=result.response.input_tokens,
                    output_tokens=result.response.output_tokens,
                    cost_usd=result.cost_usd,
                )
            )
            sp.set_attributes(
                safe_attributes(
                    {
                        "llm.input_tokens": result.response.input_tokens,
                        "llm.output_tokens": result.response.output_tokens,
                        "llm.attempts": result.attempts,
                        "llm.fallback_used": result.fallback_used,
                    }
                )
            )
            return result

    async def _complete(self, request: LLMRequest, *, tenant_id: uuid.UUID) -> LLMResult:
        await self._limiter.check(tenant_id)
        await self._budget.check(tenant_id)

        estimated = estimate_tokens(request.system) + sum(
            estimate_tokens(m.content) for m in request.messages
        )
        if estimated + request.max_output_tokens > self._max_context:
            raise LLMContextOverflow(
                estimated_input_tokens=estimated, max_context_tokens=self._max_context
            )

        total_attempts = 0
        try:
            response, used = await self._with_retries(self._primary, request)
            total_attempts += used
            return self._result(response, total_attempts, fallback_used=False)
        except _Exhausted as primary_failure:
            total_attempts += primary_failure.attempts
            cause = primary_failure.cause
            if self._fallback is None or isinstance(cause, ProviderInvalidRequest):
                raise self._to_domain(cause) from cause
            _log.warning(
                "llm_primary_failed_using_fallback",
                tenant_id=str(tenant_id),
                error=type(cause).__name__,
                primary=self._primary.name,
                fallback=self._fallback.name,
            )
            try:
                response, used = await self._with_retries(self._fallback, request)
            except _Exhausted as fallback_failure:
                raise self._to_domain(fallback_failure.cause) from fallback_failure.cause
            total_attempts += used
            return self._result(response, total_attempts, fallback_used=True)

    async def complete_json(
        self, request: LLMRequest, *, tenant_id: uuid.UUID
    ) -> tuple[dict[str, Any], LLMResult]:
        """Structured output: parse the reply as a JSON object or raise LLMInvalidResponse.
        No silent repair beyond stripping a markdown code fence."""
        result = await self.complete(request, tenant_id=tenant_id)
        text = result.response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMInvalidResponse() from exc
        if not isinstance(parsed, dict):
            raise LLMInvalidResponse()
        return parsed, result

    # ------------------------------------------------------------------ internals

    async def _with_retries(
        self, provider: LLMProvider, request: LLMRequest
    ) -> tuple[LLMResponse, int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                response = await asyncio.wait_for(provider.complete(request), self._timeout)
                return response, attempts
            except TimeoutError:
                err: ProviderError = ProviderTimeout("attempt timed out")
            except (ProviderInvalidRequest, ProviderInvalidResponse, ProviderAuthError) as exc:
                raise _Exhausted(exc, attempts) from exc
            except ProviderTransientError as exc:
                err = exc
            if attempts > self._max_retries:
                raise _Exhausted(err, attempts)
            delay = self._delay(attempts, err)
            LLM_RETRIES.labels(provider.name).inc()
            _log.info(
                "llm_retry",
                provider=provider.name,
                attempt=attempts,
                delay_seconds=round(delay, 3),
                error=type(err).__name__,
            )
            await self._sleep(delay)

    def _delay(self, attempt: int, err: ProviderError) -> float:
        backoff = min(self._cap, self._base * (2 ** (attempt - 1)))
        jittered = self._rng.uniform(0, backoff)  # full jitter
        if isinstance(err, ProviderRateLimited) and err.retry_after_seconds is not None:
            return min(self._cap, max(jittered, err.retry_after_seconds))
        return jittered

    def _result(self, response: LLMResponse, attempts: int, *, fallback_used: bool) -> LLMResult:
        cost = compute_cost_usd(
            self._pricing,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
        return LLMResult(
            response=response, cost_usd=cost, attempts=attempts, fallback_used=fallback_used
        )

    @staticmethod
    def _to_domain(err: ProviderError) -> Exception:
        if isinstance(err, ProviderInvalidResponse):
            return LLMInvalidResponse()
        if isinstance(err, ProviderTimeout):
            return LLMTimeout()
        # InvalidRequest, auth failure and exhausted transient errors all surface as one
        # controlled "unavailable" failure; the specific cause stays in logs only.
        return LLMUnavailable()


_OUTCOMES = {
    "RateLimitExceeded": "rate_limited",
    "BudgetExceeded": "budget_exceeded",
    "LLMContextOverflow": "context_overflow",
    "LLMTimeout": "timeout",
    "LLMInvalidResponse": "invalid_response",
    "LLMUnavailable": "unavailable",
}


class _Exhausted(Exception):
    def __init__(self, cause: ProviderError, attempts: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.attempts = attempts
