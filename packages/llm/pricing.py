"""
Cost accounting (Section 37). Prices are CONFIGURATION, not code: this module ships no
price table, because vendor prices change and a baked-in number would be an unverifiable
claim. A model with no configured price yields cost=None ("NOT CONFIGURED"), never 0.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    input_usd_per_million_tokens: float
    output_usd_per_million_tokens: float

    def __post_init__(self) -> None:
        if self.input_usd_per_million_tokens < 0 or self.output_usd_per_million_tokens < 0:
            raise ValueError("prices must be non-negative")


def compute_cost_usd(
    pricing: dict[str, ModelPricing], *, model: str, input_tokens: int, output_tokens: int
) -> float | None:
    p = pricing.get(model)
    if p is None:
        return None
    return (
        input_tokens * p.input_usd_per_million_tokens
        + output_tokens * p.output_usd_per_million_tokens
    ) / 1_000_000
