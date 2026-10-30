from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from packages.investigation.correlation import (
    DeploymentFact,
    IncidentFact,
    TicketFact,
    deployments_before,
    incident_overlaps_ticket,
    rank_candidates,
)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)


def _t(opened: datetime, service: str = "payments", resolved: datetime | None = None) -> TicketFact:
    return TicketFact(uuid.uuid4(), service, opened, resolved, "open", None)


def _i(
    start: datetime, end: datetime | None, service: str = "payments", sev: str = "SEV3"
) -> IncidentFact:
    return IncidentFact(uuid.uuid4(), service, sev, "open", start, end, None)


def _d(at: datetime, service: str = "payments", status: str = "succeeded") -> DeploymentFact:
    return DeploymentFact(uuid.uuid4(), service, "2.0", "prod", status, at)


def test_overlap_requires_same_service() -> None:
    t = _t(NOW)
    assert incident_overlaps_ticket(_i(NOW - H, NOW + H), t, grace=H, now=NOW)
    assert not incident_overlaps_ticket(_i(NOW - H, NOW + H, "billing"), t, grace=H, now=NOW)


def test_overlap_boundaries_and_grace() -> None:
    t = _t(NOW)
    ended_long_before = _i(NOW - 10 * H, NOW - 5 * H)
    assert not incident_overlaps_ticket(ended_long_before, t, grace=H, now=NOW)
    ended_just_before = _i(NOW - 5 * H, NOW - timedelta(minutes=30))
    assert incident_overlaps_ticket(ended_just_before, t, grace=H, now=NOW)  # within grace
    starts_after = _i(NOW + 5 * H, None)
    assert not incident_overlaps_ticket(starts_after, t, grace=H, now=NOW)


def test_open_ended_incident_runs_until_now() -> None:
    assert incident_overlaps_ticket(_i(NOW - 3 * H, None), _t(NOW), grace=H, now=NOW)


def test_deployments_before_window_and_order() -> None:
    near, far, after, other = (
        _d(NOW - H),
        _d(NOW - 5 * H),
        _d(NOW + H),
        _d(NOW - H, "billing"),
    )
    out = deployments_before(
        NOW, "payments", [far, near, after, other], lookback=timedelta(hours=24)
    )
    assert out == [near, far]  # closest first; future and other-service excluded


def test_deployment_outside_lookback_excluded() -> None:
    assert (
        deployments_before(NOW, "payments", [_d(NOW - 30 * H)], lookback=timedelta(hours=24)) == []
    )


def test_incident_with_two_tickets_is_strong_one_ticket_sev3_is_moderate() -> None:
    inc = _i(NOW - H, None)
    strong = rank_candidates([_t(NOW), _t(NOW)], [inc], [], now=NOW)
    assert [c.strength for c in strong if c.kind == "incident"] == ["strong"]
    moderate = rank_candidates([_t(NOW)], [inc], [], now=NOW)
    assert [c.strength for c in moderate if c.kind == "incident"] == ["moderate"]


def test_sev1_single_ticket_is_strong() -> None:
    c = rank_candidates([_t(NOW)], [_i(NOW - H, None, sev="SEV1")], [], now=NOW)
    assert c[0].strength == "strong"


def test_failed_deploy_shortly_before_ticket_is_strong() -> None:
    cs = rank_candidates([_t(NOW)], [], [_d(NOW - 2 * H, status="failed")], now=NOW)
    assert len(cs) == 1 and cs[0].kind == "deployment" and cs[0].strength == "strong"
    assert "failed" in cs[0].reason and "2.0h before ticket opening" in cs[0].reason


def test_successful_old_deploy_without_incident_is_moderate() -> None:
    cs = rank_candidates([_t(NOW)], [], [_d(NOW - 10 * H)], now=NOW)
    assert cs[0].strength == "moderate"


def test_deploy_before_overlapping_incident_is_strong_and_linked() -> None:
    inc = _i(NOW - 2 * H, None)
    cs = rank_candidates([_t(NOW)], [inc], [_d(NOW - 3 * H)], now=NOW)
    dep = next(c for c in cs if c.kind == "deployment")
    assert dep.strength == "strong" and dep.related_incident_ids == (inc.id,)
    assert "incident start" in dep.reason


def test_unrelated_service_produces_no_candidates() -> None:
    cs = rank_candidates(
        [_t(NOW)], [_i(NOW - H, None, "billing")], [_d(NOW - H, "billing")], now=NOW
    )
    assert cs == []


def test_same_deployment_listed_once() -> None:
    d = _d(NOW - 3 * H)
    cs = rank_candidates([_t(NOW), _t(NOW - H)], [_i(NOW - 2 * H, None)], [d], now=NOW)
    assert sum(1 for c in cs if c.ref_id == d.id) == 1


def test_ranking_strong_before_moderate_then_most_recent() -> None:
    cs = rank_candidates(
        [_t(NOW)], [], [_d(NOW - 10 * H), _d(NOW - 2 * H, status="failed")], now=NOW
    )
    assert [c.strength for c in cs] == ["strong", "moderate"]


def test_no_tickets_no_candidates() -> None:
    assert rank_candidates([], [_i(NOW - H, None)], [_d(NOW - H)], now=NOW) == []
