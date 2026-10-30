"""Kafka client security config: SASL credentials reach the clients; prod rejects unsafe setups."""

from __future__ import annotations

import pytest
from packages.config.settings import KafkaSettings, Settings
from packages.kafka.client_config import base_client_config
from pydantic import SecretStr, ValidationError


def _kafka(**kw: object) -> KafkaSettings:
    return KafkaSettings(bootstrap_servers="kafka-0.kafka:9092", **kw)  # type: ignore[arg-type]


def test_plain_config_has_only_connection_keys() -> None:
    cfg = base_client_config(_kafka(security_protocol="SSL"))
    assert cfg == {"bootstrap.servers": "kafka-0.kafka:9092", "security.protocol": "SSL"}


def test_sasl_credentials_and_ca_are_passed_through() -> None:
    cfg = base_client_config(
        _kafka(
            security_protocol="SASL_SSL",
            sasl_mechanism="SCRAM-SHA-512",
            sasl_username="ekc",
            sasl_password=SecretStr("s3cr3t"),
            ssl_ca_location="/etc/ekc/kafka-ca/ca.crt",
        )
    )
    assert cfg["sasl.mechanism"] == "SCRAM-SHA-512"
    assert cfg["sasl.username"] == "ekc"
    assert cfg["sasl.password"] == "s3cr3t"
    assert cfg["ssl.ca.location"] == "/etc/ekc/kafka-ca/ca.crt"


def test_password_never_in_repr() -> None:
    k = _kafka(sasl_password=SecretStr("s3cr3t"))
    assert "s3cr3t" not in repr(k)


def _fresh_public_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _prod(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    base = {
        "SECURITY_JWT_PUBLIC_KEY": _fresh_public_key(),
        "ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql+asyncpg://u:p@pg.data.svc:5432/ekc?ssl=require",
        "REDIS_URL": "rediss://redis.data.svc:6379/0",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka-0.kafka.data.svc:9092",
        "OBJECT_STORE_BUCKET": "b",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    return Settings()  # type: ignore[call-arg]


def test_production_sasl_without_credentials_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="SASL"):
        _prod(monkeypatch, KAFKA_SECURITY_PROTOCOL="SASL_SSL")


def test_production_plaintext_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="PLAINTEXT"):
        _prod(monkeypatch, KAFKA_SECURITY_PROTOCOL="PLAINTEXT")


def test_production_sasl_with_credentials_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _prod(
        monkeypatch,
        KAFKA_SECURITY_PROTOCOL="SASL_SSL",
        KAFKA_SASL_MECHANISM="SCRAM-SHA-512",
        KAFKA_SASL_USERNAME="ekc",
        KAFKA_SASL_PASSWORD="pw",
    )
    assert s.kafka.sasl_username == "ekc"
