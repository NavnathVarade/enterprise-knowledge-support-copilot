"""
Runtime behaviour per environment (ADR-011): what the API process does at startup, in
readiness, and at shutdown when Kafka is disabled (development) or required (production-like).

The app is built from real Settings under a controlled environment; Kafka client classes are
replaced by tripwires, so any attempt to build or contact a broker fails the test.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from apps.api.app.main import create_app
from httpx import ASGITransport, AsyncClient
from packages.config.settings import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def development_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """ENVIRONMENT=development with Kafka variables absent (the laptop situation)."""
    for key in ("KAFKA_BOOTSTRAP_SERVERS", "KAFKA_SECURITY_PROTOCOL", "KAFKA_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("OBJECT_STORE_LOCAL_DIRECTORY", str(tmp_path / "objects"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _kafka_tripwires(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def trip(name: str) -> Any:
        def _boom(*args: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"Kafka client {name} constructed while Kafka is disabled")

        return _boom

    import confluent_kafka
    import confluent_kafka.admin

    monkeypatch.setattr(confluent_kafka, "Producer", trip("Producer"))
    monkeypatch.setattr(confluent_kafka, "Consumer", trip("Consumer"))
    monkeypatch.setattr(confluent_kafka.admin, "AdminClient", trip("AdminClient"))
    return calls


async def test_development_without_kafka_starts_serves_and_shuts_down_cleanly(
    development_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _kafka_tripwires(monkeypatch)
    lookups: list[str] = []
    real = socket.getaddrinfo

    def spy(host: Any, *args: Any, **kwargs: Any) -> Any:
        lookups.append(str(host))
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", spy)

    app = create_app()
    async with app.router.lifespan_context(app):
        assert get_settings().local_event_dispatch_enabled
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == "local-event-dispatcher"]
        assert len(tasks) == 1 and not tasks[0].done()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            ready = await client.get("/api/v1/health/ready")
            live = await client.get("/api/v1/health/live")
    body = ready.json()
    assert ready.status_code == 200, body
    assert body["checks"]["kafka"] == "disabled"
    assert body["checks"]["database"] == "ok"
    assert live.status_code == 200
    assert calls == []
    assert not any("kafka" in host.lower() for host in lookups)
    assert tasks[0].done()  # shutdown stopped the dispatcher; nothing leaked
    assert not any(
        t.get_name() == "local-event-dispatcher" and not t.done() for t in asyncio.all_tasks()
    )


async def test_development_with_dispatch_disabled_starts_no_background_task(
    development_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVENTS_LOCAL_DISPATCH", "false")
    get_settings.cache_clear()
    app = create_app()
    async with app.router.lifespan_context(app):
        assert not [t for t in asyncio.all_tasks() if t.get_name() == "local-event-dispatcher"]


@pytest.fixture
def kafka_enabled_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Kafka required (as in production) but, in the test environment, PLAINTEXT is allowed."""
    monkeypatch.setenv("KAFKA_ENABLED", "true")
    monkeypatch.setenv(
        "KAFKA_BOOTSTRAP_SERVERS", os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_kafka_check(monkeypatch: pytest.MonkeyPatch, *, reachable: bool) -> None:
    async def fake(settings: Any, *, timeout_seconds: float = 3.0) -> None:
        if not reachable:
            raise ConnectionError("broker unreachable")

    monkeypatch.setattr("packages.kafka.health.check_kafka_connection", fake)


async def _ready(app: Any) -> Any:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        return await client.get("/api/v1/health/ready")


async def test_enabled_kafka_that_answers_is_reported_ok(
    kafka_enabled_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_kafka_check(monkeypatch, reachable=True)
    resp = await _ready(create_app())
    assert resp.status_code == 200 and resp.json()["checks"]["kafka"] == "ok"


async def test_enabled_kafka_outage_is_never_reported_healthy(
    kafka_enabled_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_kafka_check(monkeypatch, reachable=False)
    resp = await _ready(create_app())
    body = resp.json()
    assert body["checks"]["kafka"].startswith("degraded")  # the API only writes the outbox
    assert body["status"] == "degraded"
    assert "broker unreachable" not in resp.text  # class name only, no detail


async def test_required_kafka_outage_fails_readiness(
    kafka_enabled_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEALTH_KAFKA_REQUIRED", "true")
    get_settings.cache_clear()
    _patch_kafka_check(monkeypatch, reachable=False)
    resp = await _ready(create_app())
    assert resp.status_code == 503
    assert resp.json()["checks"]["kafka"].startswith("unavailable")


async def test_readiness_exposes_no_connection_details(
    kafka_enabled_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_kafka_check(monkeypatch, reachable=True)
    text = (await _ready(create_app())).text
    for secret in ("postgres:postgres", "localhost:5432", "redis://"):
        assert secret not in text
