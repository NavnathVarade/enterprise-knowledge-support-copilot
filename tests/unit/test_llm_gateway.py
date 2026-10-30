from __future__ import annotations

import asyncio
import random
import uuid

import pytest
from packages.domain.errors import (
    LLMContextOverflow,
    LLMInvalidResponse,
    LLMTimeout,
    LLMUnavailable,
)
from packages.llm.base import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ProviderAuthError,
    ProviderError,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTransientError,
)
from packages.llm.gateway import LLMGateway
from packages.llm.pricing import ModelPricing

pytestmark = pytest.mark.unit
TENANT = uuid.uuid4()


class Scripted:
    """Provider that plays back a list of outcomes (exception instance or text)."""

    def __init__(self, name: str, outcomes: list[object], model: str = "m") -> None:
        self.name = name
        self.model = model
        self._outcomes = list(outcomes)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            await outcome()
        return LLMResponse(
            text=str(outcome),
            provider=self.name,
            model=self.model,
            input_tokens=1000,
            output_tokens=500,
            stop_reason="end_turn",
            latency_ms=1.0,
        )


def _req(text: str = "hi", max_out: int = 100) -> LLMRequest:
    return LLMRequest(system="sys", messages=[LLMMessage("user", text)], max_output_tokens=max_out)


def _gw(
    primary: Scripted, fallback: Scripted | None = None, **kw: object
) -> tuple[LLMGateway, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    gw = LLMGateway(
        primary,
        fallback=fallback,
        sleep=fake_sleep,
        rng=random.Random(1),
        **kw,  # type: ignore[arg-type]
    )
    return gw, sleeps


async def test_success_first_try() -> None:
    p = Scripted("p", ["ok"])
    gw, sleeps = _gw(p)
    r = await gw.complete(_req(), tenant_id=TENANT)
    assert r.response.text == "ok" and r.attempts == 1 and not r.fallback_used
    assert sleeps == []


async def test_transient_retried_then_succeeds() -> None:
    p = Scripted("p", [ProviderTransientError("x"), ProviderTransientError("x"), "ok"])
    gw, sleeps = _gw(p, max_retries=2)
    r = await gw.complete(_req(), tenant_id=TENANT)
    assert r.attempts == 3 and p.calls == 3 and len(sleeps) == 2


async def test_retries_are_bounded() -> None:
    p = Scripted("p", [ProviderTransientError("x")] * 10)
    gw, _ = _gw(p, max_retries=2)
    with pytest.raises(LLMUnavailable):
        await gw.complete(_req(), tenant_id=TENANT)
    assert p.calls == 3  # 1 + 2 retries, never more


async def test_backoff_is_capped_and_jittered() -> None:
    p = Scripted("p", [ProviderTransientError("x")] * 6)
    gw, sleeps = _gw(p, max_retries=5, base_delay_seconds=1.0, max_delay_seconds=3.0)
    with pytest.raises(LLMUnavailable):
        await gw.complete(_req(), tenant_id=TENANT)
    assert len(sleeps) == 5
    assert all(0 <= s <= 3.0 for s in sleeps)
    assert len(set(sleeps)) > 1  # jitter, not a fixed schedule


async def test_retry_after_honored_but_capped() -> None:
    p = Scripted("p", [ProviderRateLimited("rl", 100.0), ProviderRateLimited("rl", 2.0), "ok"])
    gw, sleeps = _gw(p, max_retries=2, max_delay_seconds=5.0)
    await gw.complete(_req(), tenant_id=TENANT)
    assert sleeps[0] == 5.0  # 100s capped at max delay
    assert sleeps[1] >= 2.0


async def test_invalid_request_not_retried_and_no_fallback() -> None:
    p, f = Scripted("p", [ProviderInvalidRequest("bad")]), Scripted("f", ["ok"])
    gw, _ = _gw(p, f)
    with pytest.raises(LLMUnavailable):
        await gw.complete(_req(), tenant_id=TENANT)
    assert p.calls == 1 and f.calls == 0


async def test_auth_error_not_retried_but_falls_back() -> None:
    p, f = Scripted("p", [ProviderAuthError("401")]), Scripted("f", ["from fallback"])
    gw, _ = _gw(p, f)
    r = await gw.complete(_req(), tenant_id=TENANT)
    assert p.calls == 1 and r.fallback_used and r.response.text == "from fallback"


async def test_fallback_after_primary_exhausted() -> None:
    p = Scripted("p", [ProviderTransientError("x")] * 3)
    f = Scripted("f", ["fb"])
    gw, _ = _gw(p, f, max_retries=2)
    r = await gw.complete(_req(), tenant_id=TENANT)
    assert r.fallback_used and r.attempts == 4 and r.response.provider == "f"


async def test_both_fail_raises_controlled_error_not_fake_answer() -> None:
    p = Scripted("p", [ProviderTransientError("x")] * 3)
    f = Scripted("f", [ProviderTransientError("y")] * 3)
    gw, _ = _gw(p, f, max_retries=2)
    with pytest.raises(LLMUnavailable):
        await gw.complete(_req(), tenant_id=TENANT)


async def test_attempt_timeout_maps_to_llm_timeout() -> None:
    async def slow() -> None:
        await asyncio.sleep(5)

    p = Scripted("p", [slow])
    gw, _ = _gw(p, max_retries=0, attempt_timeout_seconds=0.05)
    with pytest.raises(LLMTimeout):
        await gw.complete(_req(), tenant_id=TENANT)


async def test_context_overflow_rejected_before_any_call() -> None:
    p = Scripted("p", ["ok"])
    gw, _ = _gw(p, max_context_tokens=100)
    with pytest.raises(LLMContextOverflow):
        await gw.complete(_req("x" * 4000), tenant_id=TENANT)
    assert p.calls == 0


async def test_cost_computed_when_priced_and_none_when_not() -> None:
    p = Scripted("p", ["ok", "ok"], model="priced")
    gw, _ = _gw(p, pricing={"priced": ModelPricing(3.0, 15.0)})
    r = await gw.complete(_req(), tenant_id=TENANT)
    assert r.cost_usd == pytest.approx((1000 * 3.0 + 500 * 15.0) / 1_000_000)
    gw2, _ = _gw(Scripted("p", ["ok"], model="unpriced"))
    assert (await gw2.complete(_req(), tenant_id=TENANT)).cost_usd is None


async def test_complete_json_parses_plain_and_fenced() -> None:
    gw, _ = _gw(Scripted("p", ['{"a": 1}', '```json\n{"b": 2}\n```']))
    obj, _ = await gw.complete_json(_req(), tenant_id=TENANT)
    assert obj == {"a": 1}
    obj2, _ = await gw.complete_json(_req(), tenant_id=TENANT)
    assert obj2 == {"b": 2}


@pytest.mark.parametrize("bad", ["not json", "[1,2]", "```\n```"])
async def test_complete_json_rejects_invalid(bad: str) -> None:
    gw, _ = _gw(Scripted("p", [bad]))
    with pytest.raises(LLMInvalidResponse):
        await gw.complete_json(_req(), tenant_id=TENANT)


def test_provider_error_hierarchy() -> None:
    assert issubclass(ProviderRateLimited, ProviderTransientError)
    assert not issubclass(ProviderAuthError, ProviderTransientError)
    assert issubclass(ProviderAuthError, ProviderError)


def test_negative_price_rejected() -> None:
    with pytest.raises(ValueError):
        ModelPricing(-1.0, 1.0)
