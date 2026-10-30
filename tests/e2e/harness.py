"""Runs the REAL API for browser end-to-end tests, with only the external services replaced.

    python -m tests.e2e.harness --port 18000 --state-file /tmp/state.json

Real, unmodified code: the FastAPI app and every middleware, authentication, the database layer
(real PostgreSQL + pgvector), Redis protections, the outbox relay, the ingestion handler
(parse -> chunk -> embed -> index), retrieval, context building, citation validation, usage
metering and the audit hash chain.

Stand-ins, and what that means for what E2E proves:
  * Kafka broker  -> an in-process loopback client behind the real `EventProducer`: the relay
                     publishes, the loopback "delivers" to the ingestion handler. Proves wiring,
                     NOT broker behaviour (partitions, rebalances, redelivery).
  * S3            -> `InMemoryObjectStore` (single process: fine here, never in production).
  * LLM           -> an extractive stand-in that quotes the first line of source S1 with a
                     citation. Proves the PIPELINE and citation validation, NOT answer quality.
  * Embeddings    -> the deterministic local provider (hash-based, not semantic).

Seeds two tenants with fresh random passwords written (mode 0600) to --state-file.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from apps.api.app.main import create_app
from packages.config.settings import get_settings
from packages.database.models import Tenant, User
from packages.database.models.identity import Role, UserRole
from packages.database.session import get_sessionmaker
from packages.events.schemas import EventEnvelope
from packages.ingestion.pipeline import IngestionDependencies, handle_document_ingested
from packages.kafka.producer import EventProducer
from packages.kafka.topics import Topics
from packages.llm.base import LLMRequest, LLMResponse
from packages.llm.gateway import LLMGateway
from packages.observability.logging import get_logger
from packages.orchestration.query_service import QueryDependencies
from packages.security.passwords import hash_password
from packages.storage.object_store import InMemoryObjectStore
from packages.workers.outbox_relay import OutboxRelay
from rag.embeddings.deterministic_provider import DeterministicLocalEmbeddingProvider
from rag.reranking.lexical_overlap_reranker import LexicalOverlapReranker
from sqlalchemy import text

_log = get_logger(component="e2e_harness")


class ExtractiveLLM:
    """Test double standing in for the model: echoes the first non-heading line of source S1, cited."""

    name, model = "e2e-extractive", "e2e-extractive-model"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if "analytics question into a JSON query intent" in request.system:
            text = '{"entity": "unsupported"}'
        else:
            body = request.messages[0].content
            m = re.search(r'<source id="S1"[^>]*>\n(.*?)\n</source>', body, re.DOTALL)
            lines = [
                x
                for x in (m.group(1).splitlines() if m else [])
                if x.strip() and not x.startswith("#")
            ]
            text = f"{lines[0]} [S1]" if lines else "INSUFFICIENT_EVIDENCE"
        return LLMResponse(text, "e2e-extractive", "e2e-extractive-model", 120, 30, "end_turn", 2.0)


class LoopbackKafka:
    """Implements the producer-client surface the relay needs; delivery = in-process queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._pending: list[Any] = []

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any = None) -> None:
        self._loop.call_soon_threadsafe(self.queue.put_nowait, (topic, value))
        if on_delivery is not None:
            self._pending.append(on_delivery)

    def flush(self, timeout: float = 10.0) -> int:
        for cb in self._pending:
            cb(None, object())
        self._pending.clear()
        return 0


async def _seed(state_file: Path) -> dict[str, Any]:
    sm = get_sessionmaker(get_settings().database)
    run = uuid.uuid4().hex[:8]
    state: dict[str, Any] = {"run": run, "tenants": {}}
    async with sm() as s:
        for label in ("a", "b"):
            tenant = Tenant(name=f"E2E {label}", slug=f"e2e-{label}-{run}")
            s.add(tenant)
            await s.flush()
            users: dict[str, dict[str, str]] = {}
            wanted = (
                [("admin", "admin"), ("analyst", ""), ("auditor", "security_auditor")]
                if label == "a"
                else [("analyst", "")]
            )
            roles: dict[str, Role] = {}
            for who, role_name in wanted:
                password = secrets.token_urlsafe(18)
                user = User(
                    tenant_id=tenant.id,
                    email=f"{who}-{label}-{run}@example.com",
                    hashed_password=hash_password(password),
                    display_name=who,
                )
                s.add(user)
                await s.flush()
                if role_name:
                    if role_name not in roles:
                        roles[role_name] = Role(tenant_id=tenant.id, name=role_name)
                        s.add(roles[role_name])
                        await s.flush()
                    s.add(
                        UserRole(user_id=user.id, role_id=roles[role_name].id, tenant_id=tenant.id)
                    )
                users[who] = {"email": user.email, "password": password}
            state["tenants"][label] = {"slug": tenant.slug, "users": users}
        await s.commit()
    await asyncio.to_thread(_write_private, state_file, json.dumps(state))
    return state


def _write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


async def _relay_loop(relay: OutboxRelay, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await relay.run_once()
        except Exception:  # noqa: BLE001
            _log.exception("e2e_relay_failed")
        await asyncio.sleep(0.2)


async def _consumer_loop(
    kafka: LoopbackKafka, deps: IngestionDependencies, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            topic, value = await asyncio.wait_for(kafka.queue.get(), timeout=0.5)
        except TimeoutError:
            continue
        if topic != Topics.DOCUMENT_INGESTED:
            continue  # downstream topics have no consumer in this stack
        try:
            await handle_document_ingested(deps, EventEnvelope.model_validate_json(value))
        except Exception:  # noqa: BLE001 -- the real consumer would retry/dead-letter
            _log.exception("e2e_ingestion_failed")


async def _discard_stale_outbox() -> int:
    """Marks leftover unpublished outbox rows (from other test runs) as published so this run's
    relay only handles its own events. Refuses to touch anything but a disposable database."""
    settings = get_settings()
    db_name = settings.database.url.path.lstrip("/") if settings.database.url.path else ""
    if settings.environment.value not in {"test", "development"} or "test" not in db_name:
        raise SystemExit("E2E harness refuses to run against a non-test environment/database")
    async with get_sessionmaker(settings.database)() as s:
        res = await s.execute(
            text("UPDATE outbox_events SET published_at = now() WHERE published_at IS NULL")
        )
        await s.commit()
        return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def main_async(port: int, state_file: Path) -> None:
    settings = get_settings()
    await _discard_stale_outbox()
    store = InMemoryObjectStore()
    embedder = DeterministicLocalEmbeddingProvider(dimensions=settings.embedding.dimensions)
    sm = get_sessionmaker(settings.database)
    await _seed(state_file)

    app = create_app()
    app.state.object_store = store
    app.state.query_dependencies = QueryDependencies(
        gateway=LLMGateway(ExtractiveLLM()),
        embedding_provider=embedder,
        reranker=LexicalOverlapReranker(),
        analytics_executor=None,
    )
    kafka = LoopbackKafka()
    relay = OutboxRelay(sm, EventProducer(kafka))
    deps = IngestionDependencies(
        sessionmaker=sm,
        store=store,
        embedding_provider=embedder,
        ingestion_settings=settings.ingestion,
    )
    stop = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
    )
    tasks = [
        asyncio.create_task(_relay_loop(relay, stop)),
        asyncio.create_task(_consumer_loop(kafka, deps, stop)),
    ]
    try:
        await server.serve()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--state-file", type=Path, required=True)
    args = ap.parse_args()
    os.environ.setdefault("HARDENING_LOGIN_MAX_FAILURES_PER_IP", "100000")
    os.environ.setdefault("SIGNUP_MAX_PER_IP", "100000")  # e2e signs up several organizations
    asyncio.run(main_async(args.port, args.state_file))


if __name__ == "__main__":
    main()
