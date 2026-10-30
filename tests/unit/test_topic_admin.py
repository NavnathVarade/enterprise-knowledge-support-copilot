from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any

import pytest
from packages.kafka.topic_admin import desired_topics, provision_topics
from packages.kafka.topics import Topics


class FakeAdmin:
    def __init__(self, existing: dict[str, int], fail: dict[str, Exception] | None = None) -> None:
        self._existing = existing
        self._fail = fail or {}
        self.created: list[str] = []

    def list_topics(self, timeout: float = 0) -> Any:
        return SimpleNamespace(
            topics={
                n: SimpleNamespace(partitions=dict.fromkeys(range(p)))
                for n, p in self._existing.items()
            }
        )

    def create_topics(self, new_topics: list[Any], **kwargs: Any) -> dict[str, Future[None]]:
        out: dict[str, Future[None]] = {}
        for nt in new_topics:
            f: Future[None] = Future()
            if nt.topic in self._fail:
                f.set_exception(self._fail[nt.topic])
            else:
                self.created.append(nt.topic)
                f.set_result(None)
            out[nt.topic] = f
        return out


def test_desired_topics_shape() -> None:
    specs = desired_topics(partitions=12, replication_factor=3)
    names = [s.name for s in specs]
    assert len(specs) == 9 and len(set(names)) == 9
    assert Topics.dlq_topic_for(Topics.DOCUMENT_INGESTED) in names
    dlq = next(s for s in specs if s.name == Topics.dlq_topic_for(Topics.DOCUMENT_INGESTED))
    assert dlq.partitions == 3 and dlq.config["retention.ms"] == str(30 * 86_400_000)
    assert all(s.config["min.insync.replicas"] == "2" for s in specs)


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        desired_topics(partitions=0)
    with pytest.raises(ValueError):
        desired_topics(replication_factor=2, min_isr=3)


def test_single_broker_gets_min_isr_one() -> None:
    assert all(s.config["min.insync.replicas"] == "1" for s in desired_topics(replication_factor=1))


def test_creates_missing_and_is_idempotent() -> None:
    specs = desired_topics(partitions=4, replication_factor=1)
    admin = FakeAdmin({})
    r = provision_topics(admin, specs)
    assert sorted(r.created) == sorted(s.name for s in specs) and not r.failed
    again = provision_topics(FakeAdmin({s.name: s.partitions for s in specs}), specs)
    assert again.created == [] and len(again.existing) == len(specs) and again.drift == []


def test_drift_is_reported_not_changed() -> None:
    specs = desired_topics(partitions=4, replication_factor=1)
    existing = {s.name: s.partitions for s in specs}
    existing[specs[0].name] = 2
    admin = FakeAdmin(existing)
    r = provision_topics(admin, specs)
    assert admin.created == [] and len(r.drift) == 1 and specs[0].name in r.drift[0]


def test_failures_surface_and_races_are_tolerated() -> None:
    specs = desired_topics(partitions=4, replication_factor=1)
    admin = FakeAdmin(
        {},
        fail={
            specs[0].name: RuntimeError("TOPIC_ALREADY_EXISTS"),
            specs[1].name: RuntimeError("INVALID_REPLICATION_FACTOR"),
        },
    )
    r = provision_topics(admin, specs)
    assert specs[0].name not in r.failed and specs[0].name not in r.created
    assert "INVALID_REPLICATION_FACTOR" in r.failed[specs[1].name]
