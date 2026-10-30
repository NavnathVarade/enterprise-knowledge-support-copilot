"""Kafka reachability probe (metadata request). Only ever called when Kafka is ENABLED."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from packages.config.settings import KafkaSettings
from packages.kafka.client_config import base_client_config


@lru_cache(maxsize=4)
def _admin(config_items: tuple[tuple[str, Any], ...]) -> Any:
    from confluent_kafka.admin import AdminClient

    return AdminClient(dict(config_items))


def _list_topics(settings: KafkaSettings, timeout: float) -> None:
    config = base_client_config(settings)
    # Fail within `timeout` instead of librdkafka's long internal retry schedule.
    config["socket.timeout.ms"] = int(timeout * 1000)
    _admin(tuple(sorted(config.items()))).list_topics(timeout=timeout)


async def check_kafka_connection(settings: KafkaSettings, *, timeout_seconds: float = 3.0) -> None:
    """Raises if no broker answers a metadata request. Never logs the configuration."""
    await asyncio.to_thread(_list_topics, settings, timeout_seconds)
