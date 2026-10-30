"""
LLM provider abstraction (Section 37). Business logic depends on `LLMProvider`, never on
a vendor SDK. Providers raise only the `ProviderError` family below so the gateway can
decide retry/fallback purely from the error CLASS, not by parsing vendor messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    system: str
    messages: list[LLMMessage]
    max_output_tokens: int = 1024
    temperature: float = 0.0
    # Free-form labels for logs/accounting. Must never contain prompt text.
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    latency_ms: float


class ProviderError(Exception):
    """Base for everything a provider may raise."""


class ProviderTransientError(ProviderError):
    """Timeouts, network failures, 5xx/overload. Retry, then fallback."""


class ProviderTimeout(ProviderTransientError):
    """The request exceeded its time budget."""


class ProviderRateLimited(ProviderTransientError):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderAuthError(ProviderError):
    """Bad/missing credentials. Not retried (won't self-heal); fallback may help."""


class ProviderInvalidRequest(ProviderError):
    """Malformed request or context overflow. Neither retry nor fallback will fix it."""


class ProviderInvalidResponse(ProviderError):
    """Provider answered 2xx but the body was unusable."""


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
