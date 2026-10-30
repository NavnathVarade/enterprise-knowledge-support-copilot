"""
Kafka topic provisioning (Spec §44-47). The brokers run with auto-create DISABLED, so
without this job the first publish fails. Idempotent: existing topics are left alone (a
partition-count or config drift is REPORTED, never silently changed -- shrinking partitions is
impossible and growing them reshuffles key->partition mapping, i.e. breaks per-document order).

`AdminClientLike` is a Protocol so the logic is tested with a fake; the real
`confluent_kafka.admin.AdminClient` call path is reviewed, NOT executed against a broker.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from packages.kafka.topics import Topics

_DAY_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    replication_factor: int
    config: Mapping[str, str] = field(default_factory=dict)


def desired_topics(
    *, partitions: int = 12, replication_factor: int = 3, min_isr: int | None = None
) -> list[TopicSpec]:
    """Main, retry and DLQ topic for every main topic. Replication 3 / min ISR 2 means one
    broker can fail without losing acknowledged writes (producer uses acks=all)."""
    if partitions < 1 or replication_factor < 1:
        raise ValueError("partitions and replication_factor must be >= 1")
    isr = min_isr if min_isr is not None else max(1, replication_factor - 1)
    if isr > replication_factor:
        raise ValueError("min_isr cannot exceed replication_factor")
    base = {"min.insync.replicas": str(isr), "cleanup.policy": "delete"}
    specs: list[TopicSpec] = []
    for main in (
        Topics.DOCUMENT_INGESTED,
        Topics.DOCUMENT_PROCESSING_COMPLETED,
        Topics.DOCUMENT_PROCESSING_FAILED,
    ):
        specs.append(
            TopicSpec(
                main, partitions, replication_factor, {**base, "retention.ms": str(7 * _DAY_MS)}
            )
        )
        specs.append(
            TopicSpec(
                Topics.retry_topic_for(main), partitions, replication_factor,
                {**base, "retention.ms": str(3 * _DAY_MS)},
            )
        )  # fmt: skip
        # DLQs keep failures long enough for a human to look; few partitions are enough.
        specs.append(
            TopicSpec(
                Topics.dlq_topic_for(main), max(1, partitions // 4), replication_factor,
                {**base, "retention.ms": str(30 * _DAY_MS)},
            )
        )  # fmt: skip
    return specs


class AdminClientLike(Protocol):
    def create_topics(self, new_topics: list[Any], **kwargs: Any) -> dict[str, Any]: ...
    def list_topics(self, timeout: float = ...) -> Any: ...


@dataclass(frozen=True)
class ProvisionResult:
    created: list[str]
    existing: list[str]
    drift: list[str]  # human-readable partition-count mismatches on existing topics
    failed: dict[str, str]


def provision_topics(
    admin: AdminClientLike, specs: list[TopicSpec], *, timeout: float = 30.0
) -> ProvisionResult:
    # confluent_kafka's stubs do not re-export NewTopic; resolve it dynamically.
    new_topic = importlib.import_module("confluent_kafka.admin").NewTopic

    metadata = admin.list_topics(timeout=timeout)
    present: dict[str, int] = {n: len(t.partitions) for n, t in metadata.topics.items()}
    todo = [s for s in specs if s.name not in present]
    created: list[str] = []
    failed: dict[str, str] = {}
    if todo:
        futures = admin.create_topics(
            [
                new_topic(
                    s.name, num_partitions=s.partitions,
                    replication_factor=s.replication_factor, config=dict(s.config),
                )
                for s in todo
            ],
            operation_timeout=timeout,
        )  # fmt: skip
        for name, future in futures.items():
            try:
                future.result()
                created.append(name)
            except Exception as exc:  # noqa: BLE001
                if "TOPIC_ALREADY_EXISTS" in str(exc):
                    continue  # raced with another provisioner: fine
                failed[name] = str(exc)
    existing = [s.name for s in specs if s.name in present]
    drift = [
        f"{s.name}: has {present[s.name]} partitions, wanted {s.partitions}"
        for s in specs
        if s.name in present and present[s.name] != s.partitions
    ]
    return ProvisionResult(created, existing, drift, failed)
