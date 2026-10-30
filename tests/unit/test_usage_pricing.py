from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from packages.config.settings import LLMSettings, UsageSettings
from packages.providers.factory import build_pricing
from packages.usage.budget import BudgetLimits, default_limits, utc_day_start
from packages.usage.context import current_context, usage_context


def _llm(**kw: object) -> LLMSettings:
    return LLMSettings(model="primary", fallback_model="fallback", **kw)  # type: ignore[arg-type]


def test_no_prices_means_no_pricing_at_all() -> None:
    assert build_pricing(_llm()) == {}


def test_fallback_never_borrows_the_primary_price() -> None:
    p = build_pricing(_llm(input_usd_per_million_tokens=3, output_usd_per_million_tokens=15))
    assert set(p) == {"primary"}  # fallback stays unpriced -> cost NULL


def test_each_model_gets_its_own_price() -> None:
    p = build_pricing(
        _llm(
            input_usd_per_million_tokens=3,
            output_usd_per_million_tokens=15,
            fallback_input_usd_per_million_tokens=1,
            fallback_output_usd_per_million_tokens=5,
        )
    )
    assert p["primary"].output_usd_per_million_tokens == 15
    assert p["fallback"].output_usd_per_million_tokens == 5


def test_partial_fallback_price_is_ignored() -> None:
    assert build_pricing(_llm(fallback_input_usd_per_million_tokens=1)) == {}


def test_default_limits_from_settings() -> None:
    assert default_limits(UsageSettings()).unlimited
    lim = default_limits(
        UsageSettings(default_daily_token_limit=1000, default_daily_cost_usd_limit=2.5)
    )
    assert (
        lim == BudgetLimits(1000, lim.daily_cost_usd_limit)
        and str(lim.daily_cost_usd_limit) == "2.5"
    )


def test_utc_day_start_handles_other_timezones() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    # 02:00 IST on the 18th is 20:30 UTC on the 17th
    assert utc_day_start(datetime(2026, 10, 18, 2, 0, tzinfo=ist)) == datetime(
        2026, 10, 17, tzinfo=UTC
    )


def test_usage_context_is_scoped_and_restored() -> None:
    assert current_context().operation == "other"
    uid = uuid.uuid4()
    with usage_context(operation="query", user_id=uid, request_id="r1"):
        assert current_context().user_id == uid
        with usage_context(operation="ingestion"):
            assert current_context().operation == "ingestion"
        assert current_context().operation == "query"
    assert current_context().user_id is None


def test_negative_price_rejected() -> None:
    with pytest.raises(ValueError):
        _llm(fallback_input_usd_per_million_tokens=-1)
