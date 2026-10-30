"""Single source of truth for librdkafka connection settings (producer, consumer, admin)."""

from __future__ import annotations

from typing import Any

from packages.config.settings import KafkaSettings


def base_client_config(settings: KafkaSettings) -> dict[str, Any]:
    """Connection + security keys shared by every Kafka client. The SASL password is
    unwrapped only here, at the point of handing it to the client library."""
    cfg: dict[str, Any] = {
        "bootstrap.servers": settings.require_bootstrap_servers(),
        "security.protocol": settings.security_protocol,
    }
    if settings.sasl_mechanism:
        cfg["sasl.mechanism"] = settings.sasl_mechanism
    if settings.sasl_username:
        cfg["sasl.username"] = settings.sasl_username
    if settings.sasl_password is not None:
        cfg["sasl.password"] = settings.sasl_password.get_secret_value()
    if settings.ssl_ca_location:
        cfg["ssl.ca.location"] = settings.ssl_ca_location
    return cfg
