from __future__ import annotations

import pytest
from packages.config.settings import Environment, Settings, get_settings
from packages.domain.errors import ConfigurationError
from packages.providers.factory import (
    build_analytics_executor,
    build_embedding_provider,
    build_llm_gateway,
    build_reranker,
    validate_provider_configuration,
)
from pydantic import SecretStr
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.embeddings.voyage_provider import VoyageEmbeddingProvider
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from rag.reranking.voyage_reranker import VoyageReranker

pytestmark = pytest.mark.unit


def _settings(
    env: Environment, *, embedding_key: bool = False, llm_key: bool = False, bucket: bool = True
) -> Settings:
    base = get_settings()
    return base.model_copy(
        update={
            "environment": env,
            "embedding": base.embedding.model_copy(
                update={"api_key": SecretStr("k") if embedding_key else None}
            ),
            "llm": base.llm.model_copy(update={"api_key": SecretStr("k") if llm_key else None}),
            "object_store": base.object_store.model_copy(
                update={"bucket": "ekc-docs" if bucket else None}
            ),
        }
    )


def test_production_refuses_to_start_without_real_embedding_key() -> None:
    with pytest.raises(ConfigurationError):
        validate_provider_configuration(_settings(Environment.production, llm_key=True))
    with pytest.raises(ConfigurationError):
        build_embedding_provider(_settings(Environment.production, llm_key=True))


def test_production_refuses_to_start_without_llm_key() -> None:
    with pytest.raises(ConfigurationError):
        validate_provider_configuration(_settings(Environment.staging, embedding_key=True))


def test_production_refuses_to_start_without_object_store_bucket() -> None:
    with pytest.raises(ConfigurationError, match="OBJECT_STORE_BUCKET"):
        validate_provider_configuration(
            _settings(Environment.production, embedding_key=True, llm_key=True, bucket=False)
        )
    validate_provider_configuration(_settings(Environment.test, bucket=False))


def test_production_with_keys_validates() -> None:
    validate_provider_configuration(
        _settings(Environment.production, embedding_key=True, llm_key=True)
    )


def test_dev_and_test_fall_back_to_reference_embedding() -> None:
    p = build_embedding_provider(_settings(Environment.test))
    assert isinstance(p, DeterministicLocalEmbeddingProvider)
    validate_provider_configuration(_settings(Environment.development))


def test_real_providers_selected_when_key_present() -> None:
    s = _settings(Environment.production, embedding_key=True, llm_key=True)
    assert isinstance(build_embedding_provider(s), VoyageEmbeddingProvider)
    assert isinstance(build_reranker(s), VoyageReranker)
    assert isinstance(build_reranker(_settings(Environment.test)), LexicalOverlapReranker)


def test_llm_gateway_requires_key() -> None:
    with pytest.raises(ConfigurationError):
        build_llm_gateway(_settings(Environment.test))
    assert build_llm_gateway(_settings(Environment.test, llm_key=True)) is not None


def test_analytics_disabled_in_production_without_read_only_url() -> None:
    assert build_analytics_executor(_settings(Environment.production, embedding_key=True)) is None
    assert build_analytics_executor(_settings(Environment.test)) is not None


def test_api_keys_are_not_exposed_in_repr() -> None:
    s = _settings(Environment.production, embedding_key=True, llm_key=True)
    assert "SecretStr" in repr(s.llm.api_key) and "'k'" not in repr(s.llm)
