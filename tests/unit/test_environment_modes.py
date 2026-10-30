"""
Environment-aware configuration (ADR-011): development runs without Kafka, production
requires it, and neither can silently turn into the other.

Pure settings/policy tests: no database, no broker. Every test starts from an EMPTY relevant
environment so a developer's shell (or the exported .env.test) cannot influence the outcome.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from packages.config.settings import (
    Environment,
    KafkaSettings,
    Settings,
    SettingsLoadError,
    load_settings,
)
from pydantic import ValidationError

_PREFIXES = (
    "ENVIRONMENT",
    "DATABASE_",
    "REDIS_",
    "KAFKA_",
    "EVENTS_",
    "OBJECT_STORE_",
    "SECURITY_",
    "HARDENING_",
    "HEALTH_",
    "LLM_",
    "EMBEDDING_",
    "OTEL_",
)
_DEV_DB = "postgresql+asyncpg://ekc_app:s3cretpw@localhost:5432/ekc"
_PROD_DB = "postgresql+asyncpg://ekc_app:s3cretpw@pg.internal:5432/ekc?ssl=require"


@pytest.fixture(autouse=True)
def _empty_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(_PREFIXES):
            monkeypatch.delenv(key)
    monkeypatch.setenv("EKC_ENV_FILE", "")
    monkeypatch.setenv("SECURITY_JWT_PUBLIC_KEY", "dev-only-public-key")
    monkeypatch.setenv("SECURITY_JWT_ISSUER", "ekc-dev")
    monkeypatch.setenv("SECURITY_JWT_AUDIENCE", "ekc-api")


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _development(monkeypatch: pytest.MonkeyPatch, **extra: str) -> Settings:
    _env(monkeypatch, ENVIRONMENT="development", DATABASE_URL=_DEV_DB, **extra)
    return Settings()  # type: ignore[call-arg]


def _production_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "ENVIRONMENT": "production",
        "DATABASE_URL": _PROD_DB,
        "REDIS_URL": "rediss://redis.internal:6379/0",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka-0.internal:9093",
        "KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
        "KAFKA_SASL_USERNAME": "ekc",
        "KAFKA_SASL_PASSWORD": "kafka-pw",
        "OBJECT_STORE_BUCKET": "ekc-docs",
        **overrides,
    }
    _env(monkeypatch, **values)


# ----------------------------------------------------------------------- development


@pytest.mark.unit
def test_development_loads_with_kafka_disabled_and_no_brokers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _development(monkeypatch)
    assert settings.environment is Environment.development
    assert settings.kafka_enabled is False
    assert settings.kafka.bootstrap_servers is None
    assert settings.local_event_dispatch_enabled is True
    assert str(settings.redis.url).startswith("redis://localhost:6379")


@pytest.mark.unit
def test_development_defaults_to_local_disk_object_store(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _development(monkeypatch)
    assert settings.uses_local_object_store
    assert settings.object_store.bucket is None


@pytest.mark.unit
def test_development_with_a_bucket_keeps_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _development(monkeypatch, OBJECT_STORE_BUCKET="my-dev-bucket")
    assert not settings.uses_local_object_store


@pytest.mark.unit
def test_bucket_and_local_directory_together_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="choose one"):
        _development(monkeypatch, OBJECT_STORE_BUCKET="b", OBJECT_STORE_LOCAL_DIRECTORY="x")


@pytest.mark.unit
def test_development_can_opt_in_to_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _development(
        monkeypatch,
        KAFKA_ENABLED="true",
        KAFKA_BOOTSTRAP_SERVERS="localhost:9092",
        KAFKA_SECURITY_PROTOCOL="PLAINTEXT",
    )
    assert settings.kafka_enabled is True
    assert settings.local_event_dispatch_enabled is False  # never both: events would run twice


@pytest.mark.unit
def test_development_kafka_opt_in_without_brokers_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="KAFKA_BOOTSTRAP_SERVERS is required"):
        _development(monkeypatch, KAFKA_ENABLED="true")


@pytest.mark.unit
def test_local_dispatch_cannot_be_combined_with_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="processed twice"):
        _development(
            monkeypatch,
            KAFKA_ENABLED="true",
            KAFKA_BOOTSTRAP_SERVERS="localhost:9092",
            EVENTS_LOCAL_DISPATCH="true",
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [
        {
            "KAFKA_ENABLED": "true",
            "KAFKA_BOOTSTRAP_SERVERS": "kafka-0.kafka.default.svc.cluster.local:9092",
            "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
        },
        {"REDIS_URL": "redis://redis.default.svc.cluster.local:6379/0"},
        {
            "DATABASE_READ_ONLY_ROLE_URL": "postgresql+asyncpg://r:p@pg.default.svc.cluster.local/ekc"
        },
    ],
)
def test_development_rejects_kubernetes_hostnames_from_a_stale_env(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str]
) -> None:
    with pytest.raises(ValidationError, match="cluster-internal hostnames"):
        _development(monkeypatch, **overrides)


@pytest.mark.unit
def test_disabled_kafka_ignores_a_leftover_cluster_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stale broker variable is inert while Kafka is off: nothing ever resolves it.
    settings = _development(
        monkeypatch, KAFKA_BOOTSTRAP_SERVERS="kafka-0.kafka.default.svc.cluster.local:9092"
    )
    assert settings.kafka_enabled is False


# ----------------------------------------------------------------------- production


@pytest.mark.unit
def test_production_is_the_default_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forgotten ENVIRONMENT must be strict, never development."""
    _env(monkeypatch, DATABASE_URL=_DEV_DB)
    with pytest.raises(ValidationError):  # localhost DB + no Redis/Kafka => production rules bite
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_production_enables_and_requires_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch)
    settings = Settings()  # type: ignore[call-arg]
    assert settings.kafka_enabled is True
    assert settings.local_event_dispatch_enabled is False


@pytest.mark.unit
def test_production_rejects_missing_brokers(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS")
    with pytest.raises(ValidationError, match="KAFKA_BOOTSTRAP_SERVERS is required"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_production_treats_empty_brokers_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch, KAFKA_BOOTSTRAP_SERVERS="")
    with pytest.raises(ValidationError, match="KAFKA_BOOTSTRAP_SERVERS is required"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_production_rejects_disabling_kafka(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch, KAFKA_ENABLED="false")
    with pytest.raises(ValidationError, match="KAFKA_ENABLED=false is not permitted"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_production_requires_an_explicit_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch)
    monkeypatch.delenv("REDIS_URL")
    with pytest.raises(ValidationError, match="REDIS_URL must be set explicitly"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/ekc?ssl=require"),
        ("REDIS_URL", "rediss://localhost:6379/0"),
        ("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
    ],
)
def test_production_rejects_localhost_endpoints(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    _production_env(monkeypatch, **{var: value})
    with pytest.raises(ValidationError, match="Localhost-style hosts are not permitted"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("var", "value", "needle"),
    [
        ("EVENTS_LOCAL_DISPATCH", "true", "not permitted in staging/production"),
        ("OBJECT_STORE_LOCAL_DIRECTORY", "/tmp/x", "development-only"),  # noqa: S108
    ],
)
def test_production_rejects_development_substitutes(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str, needle: str
) -> None:
    _production_env(monkeypatch, **{var: value})
    with pytest.raises(ValidationError, match=needle):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_a_dotenv_file_cannot_downgrade_production_by_naming_development(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real environment variables beat the .env file, so a stray file cannot switch a
    deployment into development mode."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("ENVIRONMENT=development\n")
    _production_env(monkeypatch)
    monkeypatch.setenv("EKC_ENV_FILE", str(dotenv))
    settings = Settings()  # type: ignore[call-arg]
    assert settings.environment is Environment.production
    assert settings.kafka_enabled is True


@pytest.mark.unit
def test_a_dotenv_file_cannot_disable_kafka_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("KAFKA_ENABLED=false\n")
    _production_env(monkeypatch)
    monkeypatch.setenv("EKC_ENV_FILE", str(dotenv))
    with pytest.raises(ValidationError, match="KAFKA_ENABLED=false is not permitted"):
        Settings()  # type: ignore[call-arg]


# ------------------------------------------------------------- validation + hygiene


@pytest.mark.unit
def test_invalid_environment_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, ENVIRONMENT="prod-ish", DATABASE_URL=_DEV_DB)
    with pytest.raises(ValidationError, match="environment"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_environment_name_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, ENVIRONMENT="Development", DATABASE_URL=_DEV_DB)
    assert Settings().environment is Environment.development  # type: ignore[call-arg]


@pytest.mark.unit
def test_empty_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _development(
        monkeypatch,
        DATABASE_READ_ONLY_ROLE_URL="",
        KAFKA_BOOTSTRAP_SERVERS="",
        KAFKA_ENABLED="",
        REDIS_URL="",
        OBJECT_STORE_BUCKET="",
        OTEL_EXPORTER_OTLP_ENDPOINT="",
        LLM_API_KEY="",
    )
    assert settings.database.read_only_role_url is None
    assert settings.kafka.bootstrap_servers is None
    assert settings.kafka.enabled is None
    assert str(settings.redis.url).startswith("redis://localhost")  # default, not ""
    assert settings.object_store.bucket is None
    assert settings.observability.exporter_otlp_endpoint is None
    assert settings.llm.api_key is None


@pytest.mark.unit
def test_empty_environment_variable_falls_back_to_strict_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, ENVIRONMENT="", DATABASE_URL=_DEV_DB)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_dotenv_file_is_a_source_below_real_environment_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"ENVIRONMENT=development\nDATABASE_URL={_DEV_DB}\nREDIS_URL=redis://localhost:6390/1\n"
    )
    monkeypatch.setenv("EKC_ENV_FILE", str(dotenv))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6391/2")  # beats the file
    settings = Settings()  # type: ignore[call-arg]
    assert settings.environment is Environment.development
    assert ":6391/2" in str(settings.redis.url)


@pytest.mark.unit
def test_load_settings_error_names_the_setting_but_never_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, ENVIRONMENT="development", DATABASE_URL="postgres-typo://u:topsecretpw@h/db")
    with pytest.raises(SettingsLoadError) as caught:
        load_settings()
    message = str(caught.value)
    assert "topsecretpw" not in message
    assert "url" in message
    assert caught.value.__cause__ is None  # no chained pydantic error carrying input_value


@pytest.mark.unit
def test_cross_field_errors_never_echo_connection_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch, KAFKA_BOOTSTRAP_SERVERS="")
    with pytest.raises(SettingsLoadError) as caught:
        load_settings()
    assert "kafka-pw" not in str(caught.value)
    assert "s3cretpw" not in str(caught.value)
    assert "KAFKA_BOOTSTRAP_SERVERS" in str(caught.value)


# --------------------------------------------------------------------- kafka client


@pytest.mark.unit
def test_kafka_client_config_refuses_to_build_without_brokers() -> None:
    from packages.kafka.client_config import base_client_config

    with pytest.raises(SettingsLoadError, match="KAFKA_BOOTSTRAP_SERVERS"):
        base_client_config(KafkaSettings())


@pytest.mark.unit
def test_kafka_enabled_settings_produce_the_intended_client_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.kafka.client_config import base_client_config

    _production_env(monkeypatch)
    settings = Settings()  # type: ignore[call-arg]
    config = base_client_config(settings.kafka)
    assert config["bootstrap.servers"] == "kafka-0.internal:9093"
    assert config["security.protocol"] == "SASL_SSL"
    assert config["sasl.username"] == "ekc"


@pytest.mark.unit
async def test_kafka_workers_refuse_to_start_when_kafka_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.config.settings import get_settings
    from packages.workers.__main__ import _run

    _env(monkeypatch, ENVIRONMENT="development", DATABASE_URL=_DEV_DB)
    get_settings.cache_clear()
    try:
        with pytest.raises(SettingsLoadError, match="cannot run with Kafka disabled"):
            await _run("outbox-relay")
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------- alembic / psycopg URL


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_sync_url_translates_ssl_to_sslmode_for_psycopg(mode: str) -> None:
    from packages.config.settings import to_sync_database_url

    url = f"postgresql+asyncpg://ekc_app:p%40ss%2Fw%3Ard@pg.internal:5432/ekc?ssl={mode}"
    assert to_sync_database_url(url) == (
        f"postgresql+psycopg://ekc_app:p%40ss%2Fw%3Ard@pg.internal:5432/ekc?sslmode={mode}"
    )


@pytest.mark.unit
def test_sync_url_without_tls_options_only_swaps_the_driver() -> None:
    from packages.config.settings import to_sync_database_url

    url = "postgresql+asyncpg://u:p@localhost:5432/ekc"
    assert to_sync_database_url(url) == "postgresql+psycopg://u:p@localhost:5432/ekc"


@pytest.mark.unit
def test_sync_url_keeps_other_query_parameters() -> None:
    from packages.config.settings import to_sync_database_url

    out = to_sync_database_url("postgresql+asyncpg://u:p@h/db?ssl=require&application_name=ekc")
    assert "sslmode=require" in out and "application_name=ekc" in out and "ssl=" not in out


@pytest.mark.unit
def test_alembic_environment_uses_the_tls_aware_url_helper() -> None:
    """Regression: production DSNs carry `?ssl=require`, which psycopg rejects. env.py must go
    through to_sync_database_url, not a bare driver swap (found by the Phase 50 live prod run)."""
    env_py = (Path(__file__).resolve().parents[2] / "migrations" / "env.py").read_text()
    assert "to_sync_database_url(" in env_py
    assert 'replace("postgresql+asyncpg://"' not in env_py
