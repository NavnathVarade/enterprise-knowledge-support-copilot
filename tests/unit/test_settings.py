from __future__ import annotations

import pytest
from packages.config.settings import Settings
from pydantic import ValidationError


def _base_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@postgres:5432/ekc?ssl=require",
        "REDIS_URL": "rediss://redis:6379/0",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka-0:9092",
        "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
        "KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
        "KAFKA_SASL_USERNAME": "ekc",
        "KAFKA_SASL_PASSWORD": "test-password",
        "SECURITY_JWT_PUBLIC_KEY": "test-key",
        "SECURITY_JWT_ISSUER": "ekc-test",
        "SECURITY_JWT_AUDIENCE": "ekc-test",
        **overrides,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.unit
def test_settings_load_with_service_dns_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch, ENVIRONMENT="production")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.environment.value == "production"
    assert "postgres:5432" in str(settings.database.url)


@pytest.mark.unit
def test_settings_reject_localhost_database_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(
        monkeypatch,
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/ekc",
    )
    with pytest.raises(ValidationError, match="Localhost-style hosts are not permitted"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_settings_reject_localhost_redis_in_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(
        monkeypatch,
        ENVIRONMENT="staging",
        REDIS_URL="rediss://127.0.0.1:6379/0",
    )
    with pytest.raises(ValidationError, match="Localhost-style hosts are not permitted"):
        Settings()  # type: ignore[call-arg]


@pytest.mark.unit
def test_settings_allow_localhost_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(
        monkeypatch,
        ENVIRONMENT="development",
        DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/ekc",
    )
    settings = Settings()  # type: ignore[call-arg]
    assert settings.environment.value == "development"


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"DATABASE_URL": "postgresql+asyncpg://u:p@pg:5432/ekc"}, "DATABASE_URL must set"),
        (
            {"DATABASE_URL": "postgresql+asyncpg://u:p@pg:5432/ekc?ssl=disable"},
            "DATABASE_URL must set",
        ),
        ({"REDIS_URL": "redis://redis:6379/0"}, "REDIS_URL must use rediss"),
        (
            {"DATABASE_READ_ONLY_ROLE_URL": "postgresql+asyncpg://r:p@pg:5432/ekc"},
            "DATABASE_READ_ONLY_ROLE_URL must set",
        ),
    ],
)
@pytest.mark.parametrize("environment", ["staging", "production"])
def test_unencrypted_dependencies_are_refused_in_real_environments(
    monkeypatch: pytest.MonkeyPatch, environment: str, overrides: dict[str, str], needle: str
) -> None:
    _base_env(monkeypatch, ENVIRONMENT=environment, **overrides)
    with pytest.raises(ValidationError, match=needle):
        Settings()  # type: ignore[call-arg]


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_tls_modes_are_accepted(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    _base_env(
        monkeypatch,
        ENVIRONMENT="production",
        DATABASE_URL=f"postgresql+asyncpg://u:p@pg:5432/ekc?ssl={mode}",
    )
    Settings()  # type: ignore[call-arg]


def test_plaintext_requires_an_explicit_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(
        monkeypatch,
        ENVIRONMENT="production",
        DATABASE_URL="postgresql+asyncpg://u:p@pg:5432/ekc",
        REDIS_URL="redis://redis:6379/0",
        HARDENING_REQUIRE_DEPENDENCY_TLS="false",
    )
    assert Settings().hardening.require_dependency_tls is False  # type: ignore[call-arg]


def test_development_and_test_are_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(
        monkeypatch,
        ENVIRONMENT="test",
        DATABASE_URL="postgresql+asyncpg://u:p@pg:5432/ekc",
        REDIS_URL="redis://redis:6379/0",
    )
    Settings()  # type: ignore[call-arg]
