from __future__ import annotations

import random
import uuid

import pytest
from packages.domain.errors import LLMUnavailable
from packages.llm.base import LLMMessage, LLMRequest, ProviderTransientError
from packages.llm.gateway import LLMGateway
from packages.llm.pricing import ModelPricing
from packages.observability.metrics import REGISTRY
from tests.unit.test_llm_gateway import Scripted

pytestmark = pytest.mark.unit
T = uuid.uuid4()


def _req() -> LLMRequest:
    return LLMRequest(system="s", messages=[LLMMessage("user", "hi")], max_output_tokens=50)


def _v(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def _noop(_: float) -> None:
    return None


async def test_success_records_calls_tokens_and_cost_only_when_priced() -> None:
    labels = {"provider": "pm1", "model": "m1"}
    gw = LLMGateway(
        Scripted("pm1", ["ok"], model="m1"),
        pricing={"m1": ModelPricing(1.0, 2.0)},
        sleep=_noop,
    )
    await gw.complete(_req(), tenant_id=T)
    assert _v("ekc_llm_requests_total", outcome="success", **labels) == 1
    assert _v("ekc_llm_tokens_total", direction="input", **labels) == 1000
    assert _v("ekc_llm_tokens_total", direction="output", **labels) == 500
    assert _v("ekc_llm_cost_usd_total", **labels) == pytest.approx(0.002)

    unpriced = LLMGateway(Scripted("pm2", ["ok"], model="m2"), sleep=_noop)
    await unpriced.complete(_req(), tenant_id=T)
    assert _v("ekc_llm_requests_total", provider="pm2", model="m2", outcome="success") == 1
    assert _v("ekc_llm_cost_usd_total", provider="pm2", model="m2") == 0.0  # no price => no cost


async def test_retries_fallback_and_failures_are_counted() -> None:
    err = ProviderTransientError("x")
    gw = LLMGateway(
        Scripted("pm3", [err, "ok"], model="m3"), max_retries=2, sleep=_noop, rng=random.Random(1)
    )
    await gw.complete(_req(), tenant_id=T)
    assert _v("ekc_llm_retries_total", provider="pm3") == 1

    fb = LLMGateway(
        Scripted("pm4", [err, err], model="m4"),
        fallback=Scripted("pm5", ["ok"], model="m5"),
        max_retries=1,
        sleep=_noop,
    )
    await fb.complete(_req(), tenant_id=T)
    assert _v("ekc_llm_requests_total", provider="pm5", model="m5", outcome="fallback") == 1

    dead = LLMGateway(Scripted("pm6", [err, err], model="m6"), max_retries=1, sleep=_noop)
    with pytest.raises(LLMUnavailable):
        await dead.complete(_req(), tenant_id=T)
    assert _v("ekc_llm_requests_total", provider="pm6", model="m6", outcome="unavailable") == 1
