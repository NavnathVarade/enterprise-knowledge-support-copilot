"""The readiness checker must fail on bad input (else a green report means nothing) and pass on the repo."""

from __future__ import annotations

import copy
import os
import shutil
from typing import Any

import pytest
from scripts import readiness_review as rr

GOOD_CONTAINER: dict[str, Any] = {
    "name": "c",
    "image": "ekc:1.2.3",
    "securityContext": {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    },
    "resources": {"requests": {"cpu": "1m", "memory": "1Mi"}, "limits": {"memory": "2Mi"}},
    "livenessProbe": {},
    "readinessProbe": {},
}


def _docs(
    container: dict[str, Any], *, pdb: bool = True, deny: bool = True
) -> list[dict[str, Any]]:
    sel = {"matchLabels": {"app": "x"}}
    docs: list[dict[str, Any]] = [
        {
            "kind": "Deployment",
            "metadata": {"name": "x"},
            "spec": {
                "selector": sel,
                "template": {
                    "spec": {"securityContext": {"runAsNonRoot": True}, "containers": [container]}
                },
            },
        }
    ]
    if pdb:
        docs.append(
            {"kind": "PodDisruptionBudget", "metadata": {"name": "x"}, "spec": {"selector": sel}}
        )
    if deny:
        docs.append(
            {
                "kind": "NetworkPolicy",
                "metadata": {"name": "d"},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
            }
        )
    return docs


def _statuses(docs: list[dict[str, Any]]) -> dict[str, str]:
    return {c.name.split("] ", 1)[1]: c.status for c in rr.check_manifests("t", docs)}


def test_a_compliant_workload_passes_every_manifest_check() -> None:
    assert set(_statuses(_docs(GOOD_CONTAINER)).values()) == {"PASS"}


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda c: c["securityContext"].update(readOnlyRootFilesystem=False), "non-root"),
        (lambda c: c["securityContext"].update(allowPrivilegeEscalation=True), "non-root"),
        (lambda c: c["resources"].pop("limits"), "requests and a memory limit"),
        (lambda c: c.update(image="ekc:latest"), "images are pinned"),
        (lambda c: c.update(image="ekc"), "images are pinned"),
        (lambda c: c.pop("readinessProbe"), "probes"),
    ],
)
def test_each_violation_is_detected(mutate: Any, needle: str) -> None:
    c = copy.deepcopy(GOOD_CONTAINER)
    mutate(c)
    failed = [k for k, v in _statuses(_docs(c)).items() if v == "FAIL"]
    assert any(needle in k for k in failed), failed


def test_missing_pdb_and_missing_default_deny_are_detected() -> None:
    st = _statuses(_docs(GOOD_CONTAINER, pdb=False, deny=False))
    assert [
        k
        for k, v in st.items()
        if v == "FAIL" and ("PodDisruptionBudget" in k or "default-deny" in k)
    ].__len__() == 2


def test_committed_secret_data_is_detected() -> None:
    docs = _docs(GOOD_CONTAINER) + [
        {"kind": "Secret", "metadata": {"name": "s"}, "data": {"k": "dg=="}}
    ]
    assert any(v == "FAIL" and "Secret" in k for k, v in _statuses(docs).items())


def test_repository_passes_the_static_checks_that_need_no_cluster_tooling() -> None:
    for check in [
        *rr.check_dockerfiles(),
        *rr.check_migrations(),
        *rr.check_alert_runbooks(),
        *rr.check_secrets_hygiene(),
    ]:
        assert check.status == "PASS", check


def test_rendered_overlays_pass_when_kustomize_is_available() -> None:
    kustomize = os.environ.get("KUSTOMIZE_BIN") or shutil.which("kustomize")
    if not kustomize:
        pytest.skip("kustomize not available")
    failures = [
        c
        for o in ("staging", "production")
        for c in rr.check_manifests(o, rr.render(o, kustomize))
        if c.status != "PASS"
    ]
    assert not failures, failures


def test_open_risks_are_reported_never_hidden() -> None:
    (check,) = rr.open_risks()
    assert check.status == "SKIPPED" and "markers" in check.detail


def test_main_exit_code_reflects_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rr, "run_all", lambda _k: [rr.Check("a", "PASS", ""), rr.Check("b", "FAIL", "x")]
    )
    lines: list[str] = []
    assert rr.main(["--kustomize", ""], emit=lines.append) == 1
    monkeypatch.setattr(rr, "run_all", lambda _k: [rr.Check("a", "PASS", "")])
    assert rr.main(["--kustomize", ""], emit=lines.append) == 0
