"""
Provider wiring from settings (Spec §23, §27, §37).

Production rule: the deterministic embedding provider is a TEST DOUBLE whose vectors
carry no semantic meaning, so staging/production refuse to start without a real
embedding key (`ConfigurationError`) rather than silently serving meaningless retrieval.
The lexical-overlap reranker is functional but not semantic: it is allowed as a fallback
(with a loud log) so a missing rerank key degrades quality instead of availability.
The LLM provider has no fallback at all -- no key means no generation, never a fake one.
"""

from __future__ import annotations

from rag.embeddings.base import EmbeddingProvider
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.embeddings.voyage_provider import VoyageEmbeddingProvider
from rag.reranking.base import RerankerProvider
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from rag.reranking.voyage_reranker import VoyageReranker
from redis.asyncio import Redis

from packages.analytics.executor import AnalyticsExecutor
from packages.config.settings import Environment, LLMSettings, Settings
from packages.database.session import get_sessionmaker
from packages.domain.errors import ConfigurationError
from packages.llm.anthropic_provider import AnthropicProvider
from packages.llm.base import LLMProvider
from packages.llm.gateway import LLMGateway
from packages.llm.pricing import ModelPricing
from packages.llm.rate_limit import NoopRateLimiter, RateLimiter, RedisRateLimiter
from packages.observability.logging import get_logger
from packages.usage.budget import DbBudgetGuard
from packages.usage.recorder import DbUsageRecorder

_log = get_logger(component="provider_factory")
_NON_PROD = (Environment.development, Environment.test)


def validate_provider_configuration(settings: Settings) -> None:
    """Fail fast at startup in staging/production."""
    if settings.environment in _NON_PROD:
        return
    if settings.embedding.api_key is None:
        raise ConfigurationError("EMBEDDING_API_KEY is required outside development/test.")
    if settings.llm.api_key is None:
        raise ConfigurationError("LLM_API_KEY is required outside development/test.")
    if not settings.object_store.bucket:
        raise ConfigurationError("OBJECT_STORE_BUCKET is required outside development/test.")


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding.api_key is not None:
        return VoyageEmbeddingProvider(
            api_key=settings.embedding.api_key.get_secret_value(), settings=settings.embedding
        )
    if settings.environment in _NON_PROD:
        _log.warning("embedding_reference_provider_in_use", environment=settings.environment.value)
        return DeterministicLocalEmbeddingProvider(dimensions=settings.embedding.dimensions)
    raise ConfigurationError("EMBEDDING_API_KEY is required outside development/test.")


def build_reranker(settings: Settings) -> RerankerProvider:
    if settings.embedding.api_key is not None:
        return VoyageReranker(
            api_key=settings.embedding.api_key.get_secret_value(),
            model=settings.embedding.rerank_model,
        )
    _log.warning("reranker_reference_implementation_in_use", kind="lexical_overlap")
    return LexicalOverlapReranker()


def build_llm_provider(settings: Settings, *, model: str | None = None) -> LLMProvider:
    if settings.llm.api_key is None:
        raise ConfigurationError("LLM_API_KEY is required for answer generation.")
    return AnthropicProvider(
        api_key=settings.llm.api_key.get_secret_value(),
        model=model or settings.llm.model,
        timeout_seconds=settings.llm.request_timeout_seconds,
    )


def build_pricing(llm: LLMSettings) -> dict[str, ModelPricing]:
    """Per-model prices. A model without a configured price is simply absent, so its cost
    is recorded as unknown (NULL) -- the fallback never borrows the primary's rate."""
    pricing: dict[str, ModelPricing] = {}
    if (
        llm.fallback_model
        and llm.fallback_input_usd_per_million_tokens is not None
        and llm.fallback_output_usd_per_million_tokens is not None
    ):
        pricing[llm.fallback_model] = ModelPricing(
            llm.fallback_input_usd_per_million_tokens, llm.fallback_output_usd_per_million_tokens
        )
    if (
        llm.input_usd_per_million_tokens is not None
        and llm.output_usd_per_million_tokens is not None
    ):
        # Set last so the primary's price wins if both names are identical. A fallback
        # without its own price stays UNPRICED (cost NULL) rather than borrowing this one.
        pricing[llm.model] = ModelPricing(
            llm.input_usd_per_million_tokens, llm.output_usd_per_million_tokens
        )
    return pricing


def build_llm_gateway(settings: Settings, *, redis: Redis[str] | None = None) -> LLMGateway:
    llm = settings.llm
    fallback = (
        build_llm_provider(settings, model=llm.fallback_model) if llm.fallback_model else None
    )
    pricing = build_pricing(llm)
    limiter: RateLimiter = (
        RedisRateLimiter(redis, limit_per_window=settings.rate_limits.tenant_queries_per_minute)
        if redis is not None
        else NoopRateLimiter()
    )
    return LLMGateway(
        build_llm_provider(settings),
        fallback=fallback,
        max_retries=llm.max_retries,
        base_delay_seconds=llm.retry_base_delay_seconds,
        max_delay_seconds=llm.retry_max_delay_seconds,
        attempt_timeout_seconds=llm.request_timeout_seconds,
        max_context_tokens=llm.max_context_tokens,
        pricing=pricing,
        rate_limiter=limiter,
        budget_guard=(
            DbBudgetGuard(settings.database, settings.usage)
            if settings.usage.metering_enabled
            else None
        ),
        usage_recorder=(
            DbUsageRecorder(settings.database) if settings.usage.metering_enabled else None
        ),
    )


def build_analytics_executor(settings: Settings) -> AnalyticsExecutor | None:
    """
    Analytics must run as the dedicated read-only role (Spec §40): with
    DATABASE_READ_ONLY_ROLE_URL set, that DSN is used. Without it, staging/production
    DISABLE analytics (returns None) rather than query as the table owner, which would
    bypass row-level security. Development/test fall back to the main DSN, loudly.
    """
    db = settings.database
    if db.read_only_role_url is not None:
        ro_settings = db.model_copy(update={"url": db.read_only_role_url})
        return AnalyticsExecutor(
            get_sessionmaker(ro_settings), statement_timeout_ms=db.statement_timeout_ms
        )
    if settings.environment in _NON_PROD:
        _log.warning("analytics_using_main_dsn_without_readonly_role")
        return AnalyticsExecutor(get_sessionmaker(db), statement_timeout_ms=db.statement_timeout_ms)
    _log.warning("analytics_disabled_no_read_only_role_url")
    return None
