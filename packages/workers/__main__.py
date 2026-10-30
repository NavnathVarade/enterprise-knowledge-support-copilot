"""
Worker entrypoint:  python -m packages.workers ingestion | outbox-relay

Each runs until SIGTERM/SIGINT, finishing the in-flight message/batch first (graceful
shutdown for Kubernetes). Configuration comes from the environment (same Settings as the
API). Reviewed, NOT executed against a live broker in the build environment.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import signal

from packages.config.settings import SettingsLoadError, get_settings
from packages.database.session import get_sessionmaker
from packages.ingestion.pipeline import handle_document_ingested
from packages.kafka.health import check_kafka_connection
from packages.kafka.idempotency import IdempotencyGuard
from packages.kafka.producer import EventProducer, create_confluent_producer
from packages.kafka.runner import ConsumerRunner, create_confluent_consumer
from packages.kafka.topics import Topics, consumer_group_name
from packages.observability.logging import configure_logging, get_logger
from packages.observability.tracing import configure_tracing
from packages.providers.factory import validate_provider_configuration
from packages.redis.client import get_redis_client
from packages.workers.health import Heartbeat
from packages.workers.outbox_relay import OutboxRelay
from packages.workers.wiring import build_ingestion_dependencies


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    return stop


def _serve_metrics(port: int, token: str) -> None:
    """Minimal authenticated /metrics server on a daemon thread."""
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from packages.observability.metrics import render_metrics

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
            elif not hmac.compare_digest(supplied.encode(), token.encode()):
                self.send_response(401)
                self.end_headers()
            else:
                body, content_type = render_metrics()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    # Binds all interfaces on purpose: Prometheus scrapes the pod IP. Protected by the bearer
    # token above and by the NetworkPolicy (only the monitoring namespace may reach it).
    Thread(
        target=ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever,  # noqa: S104  # nosec B104
        daemon=True,
    ).start()


async def _run(role: str) -> None:
    settings = get_settings()
    configure_logging(
        service_name=f"{settings.observability.service_name}-{role}",
        environment=settings.environment.value,
        log_level=settings.observability.log_level,
    )
    validate_provider_configuration(settings)
    log = get_logger(component="worker", role=role)
    stop = _stop_event()
    if settings.observability.exporter_otlp_endpoint:
        configure_tracing(settings.observability, environment=settings.environment.value)
    token = settings.observability.metrics_bearer_token
    if token is not None:  # fail-closed: no token => no metrics port is opened
        _serve_metrics(settings.observability.metrics_port, token.get_secret_value())
    if not settings.kafka_enabled:
        raise SettingsLoadError(
            f"The '{role}' worker consumes/publishes Kafka and cannot run with Kafka disabled. "
            "In development, ingestion runs inside the API (EVENTS_LOCAL_DISPATCH); "
            "set KAFKA_ENABLED=true and KAFKA_BOOTSTRAP_SERVERS to run this worker."
        )
    # Fail the rollout (not the first message) when no broker answers.
    await check_kafka_connection(
        settings.kafka, timeout_seconds=settings.health.check_timeout_seconds
    )
    sessionmaker = get_sessionmaker(settings.database)
    producer = EventProducer(create_confluent_producer(settings.kafka))
    beat = Heartbeat().beat
    beat()

    if role == "outbox-relay":
        log.info("worker_started")
        await OutboxRelay(sessionmaker, producer, heartbeat=beat).run_forever(stop)
    else:
        deps = build_ingestion_dependencies(settings, sessionmaker)
        group = consumer_group_name(
            prefix=settings.kafka.consumer_group_prefix, logical_name="ingestion"
        )
        runner = ConsumerRunner(
            create_confluent_consumer(settings.kafka, group_id=group),
            topic=Topics.DOCUMENT_INGESTED,
            handler=functools.partial(handle_document_ingested, deps),
            idempotency_guard=IdempotencyGuard(get_redis_client(settings.redis)),
            dlq_producer=producer,
            consumer_group=group,
            heartbeat=beat,
        )
        log.info("worker_started", group=group)
        await runner.run_forever(stop)
    log.info("worker_stopped")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m packages.workers")
    parser.add_argument("role", choices=["ingestion", "outbox-relay"])
    asyncio.run(_run(parser.parse_args().role))


if __name__ == "__main__":
    main()
