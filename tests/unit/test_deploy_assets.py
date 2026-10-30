"""Policy tests over the deployment assets. Parsing-based tests always run; the ones that
need the kustomize binary skip when it is absent (CI installs it)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.gen_prometheusrule import render

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "infrastructure" / "kubernetes"
DOCKERFILE = (ROOT / "infrastructure" / "docker" / "Dockerfile").read_text()


def _kustomize() -> str | None:
    return os.environ.get("KUSTOMIZE_BIN") or shutil.which("kustomize")


def _build(overlay: str) -> list[dict[str, Any]]:
    kz = _kustomize()
    if kz is None:
        pytest.skip("kustomize not installed")
    out = subprocess.run(
        [kz, "build", str(K8S / "overlays" / overlay)], check=True, capture_output=True, text=True
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _pod_specs(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for d in docs:
        if d["kind"] == "Deployment" or d["kind"] == "Job":
            out.append((d["metadata"]["name"], d["spec"]["template"]["spec"]))
    return out


# ---- Dockerfile ---------------------------------------------------------------------------


def test_dockerfile_runs_as_non_root_numeric_user() -> None:
    users = re.findall(r"^USER\s+(\S+)", DOCKERFILE, re.M)
    assert users and users[-1] == "10001:10001"


def test_dockerfile_installs_from_the_frozen_uv_lock_and_has_no_secrets() -> None:
    assert "uv sync --frozen --no-dev" in DOCKERFILE
    assert "uv.lock" in DOCKERFILE and "pip install" not in DOCKERFILE
    assert not re.search(r"(?im)^(ENV|ARG)\s+\w*(SECRET|PASSWORD|API_KEY|TOKEN)\w*", DOCKERFILE)
    assert "COPY . " not in DOCKERFILE and "COPY .env" not in DOCKERFILE


def test_dockerignore_excludes_secrets_and_tests() -> None:
    ignore = (ROOT / ".dockerignore").read_text()
    for needle in (".env", "tests", ".git"):
        assert needle in ignore


def _lock_packages() -> list[dict[str, object]]:
    import tomllib

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return list(lock["package"])


def test_uv_lock_pins_every_dependency_exactly_from_a_registry() -> None:
    pkgs = _lock_packages()
    assert len(pkgs) > 50
    for pkg in pkgs:
        src = pkg["source"]
        assert isinstance(src, dict)
        if "editable" in src or "virtual" in src:
            continue  # the project itself
        assert "registry" in src, f"{pkg['name']} is not from a package registry"
        assert re.fullmatch(r"[0-9][0-9A-Za-z.+!_-]*", str(pkg["version"]))
        assert pkg.get("wheels") or pkg.get("sdist"), pkg["name"]


def test_uv_lock_records_hashes_for_every_artifact() -> None:
    for pkg in _lock_packages():
        for art in [*(pkg.get("wheels") or []), *([pkg["sdist"]] if pkg.get("sdist") else [])]:  # type: ignore[union-attr]
            assert str(art["hash"]).startswith("sha256:"), pkg["name"]


def test_requirements_directory_is_gone_so_there_is_one_source_of_truth() -> None:
    assert not (ROOT / "requirements" / "constraints.txt").exists()
    assert (ROOT / "uv.lock").is_file() and (ROOT / ".python-version").is_file()


# ---- manifests ----------------------------------------------------------------------------


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_every_pod_is_hardened(overlay: str) -> None:
    pods = _pod_specs(_build(overlay))
    assert len(pods) == 6  # api, web, ingestion worker, outbox relay, 2 jobs
    for name, spec in pods:
        sc = spec["securityContext"]
        assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 10001, name
        assert sc["seccompProfile"]["type"] == "RuntimeDefault", name
        assert spec["automountServiceAccountToken"] is False, name
        for c in spec["containers"]:
            csc = c["securityContext"]
            assert csc["allowPrivilegeEscalation"] is False, name
            assert csc["readOnlyRootFilesystem"] is True, name
            assert csc["capabilities"]["drop"] == ["ALL"], name
            assert "memory" in c["resources"]["limits"], name
            assert "cpu" in c["resources"]["requests"], name
            assert not c["image"].endswith(":latest") and ":" in c["image"], name


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_long_running_workloads_have_probes(overlay: str) -> None:
    for d in _build(overlay):
        if d["kind"] != "Deployment":
            continue
        c = d["spec"]["template"]["spec"]["containers"][0]
        assert "livenessProbe" in c, d["metadata"]["name"]
        if d["metadata"]["name"] in {"ekc-api", "ekc-web"}:
            assert "readinessProbe" in c and "startupProbe" in c
        else:
            assert c["livenessProbe"]["exec"]["command"][-3:-1] == [
                "packages.workers.health",
                "--max-age",
            ]


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_no_localhost_and_no_inline_secrets(overlay: str) -> None:
    docs = _build(overlay)
    cm = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert cm["ENVIRONMENT"] == overlay
    for k, v in cm.items():
        assert not re.search(r"localhost|127\.0\.0\.1|0\.0\.0\.0", v), k
        assert not re.search(r"(?i)password|secret|api_key|private_key", k), k
    assert not [d for d in docs if d["kind"] == "Secret"]
    for _, spec in _pod_specs(docs):
        for c in spec["containers"]:
            assert all(
                "value" not in e or not re.search("(?i)key|pass|token", e["name"])
                for e in c.get("env", [])
            )


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_network_policy_is_default_deny(overlay: str) -> None:
    pols = {d["metadata"]["name"]: d for d in _build(overlay) if d["kind"] == "NetworkPolicy"}
    deny = pols["default-deny-all"]["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in deny and "egress" not in deny


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_availability_objects_exist_and_are_consistent(overlay: str) -> None:
    docs = _build(overlay)
    kinds = {d["kind"] for d in docs}
    assert {"PodDisruptionBudget", "HorizontalPodAutoscaler", "Ingress", "PrometheusRule"} <= kinds
    hpa = next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")["spec"]
    pdb = next(d for d in docs if d["kind"] == "PodDisruptionBudget")["spec"]
    assert pdb["minAvailable"] < hpa["minReplicas"]


def test_migrate_job_runs_alembic_and_relay_is_singleton() -> None:
    docs = _build("production")
    mig = next(d for d in docs if d["metadata"]["name"] == "ekc-migrate")
    assert mig["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "alembic",
        "upgrade",
        "head",
    ]
    relay = next(d for d in docs if d["metadata"]["name"] == "ekc-outbox-relay")
    assert relay["spec"]["replicas"] == 1 and relay["spec"]["strategy"]["type"] == "Recreate"


# ---- Phase 36: scheduling, capacity and RBAC posture -------------------------------------


def _quantity_mi(q: str) -> int:
    m = re.fullmatch(r"(\d+)(Mi|Gi)", q)
    assert m, q
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_no_rbac_objects_and_no_api_token_in_any_pod(overlay: str) -> None:
    """Decision: the workloads never call the Kubernetes API, so they get NO Role/RoleBinding.
    Adding one later must be a deliberate, reviewed change (this test then needs updating)."""
    docs = _build(overlay)
    forbidden = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}
    assert not [d["kind"] for d in docs if d["kind"] in forbidden]
    sa = next(d for d in docs if d["kind"] == "ServiceAccount")
    assert sa["automountServiceAccountToken"] is False


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_every_container_has_bounded_ephemeral_storage(overlay: str) -> None:
    for name, spec in _pod_specs(_build(overlay)):
        sizes = [
            _quantity_mi(v["emptyDir"]["sizeLimit"]) for v in spec["volumes"] if "emptyDir" in v
        ]
        for c in spec["containers"]:
            req = c["resources"]["requests"]["ephemeral-storage"]
            lim = c["resources"]["limits"]["ephemeral-storage"]
            assert _quantity_mi(req) <= _quantity_mi(lim), name
            # the scratch volume counts against the limit: it must fit, or the pod is evicted
            assert all(s <= _quantity_mi(lim) for s in sizes), name


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_long_running_pods_spread_across_nodes_and_workers_have_startup_probes(
    overlay: str,
) -> None:
    for d in _build(overlay):
        if d["kind"] != "Deployment":
            continue
        name = d["metadata"]["name"]
        spec = d["spec"]["template"]["spec"]
        term = spec["affinity"]["podAntiAffinity"][
            "preferredDuringSchedulingIgnoredDuringExecution"
        ]
        assert term[0]["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname", name
        assert (
            term[0]["podAffinityTerm"]["labelSelector"]["matchLabels"]
            == d["spec"]["selector"]["matchLabels"]
        ), name
        probe = spec["containers"][0]["startupProbe"]
        assert probe["failureThreshold"] * probe["periodSeconds"] >= 60, name


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_api_replica_count_is_owned_by_the_hpa(overlay: str) -> None:
    api = next(
        d
        for d in _build(overlay)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "ekc-api"
    )
    assert "replicas" not in api["spec"]  # a static count would fight the autoscaler on re-apply


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_ingestion_worker_has_a_pdb_and_quota_covers_the_hpa_ceiling(overlay: str) -> None:
    docs = _build(overlay)
    pdbs = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "PodDisruptionBudget"}
    assert pdbs["ekc-ingestion-worker"]["maxUnavailable"] == 1
    hpa = next(d for d in docs if d["kind"] == "HorizontalPodAutoscaler")["spec"]
    quota = next(d for d in docs if d["kind"] == "ResourceQuota")["spec"]["hard"]
    workers = sum(
        d["spec"]["replicas"]
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] != "ekc-api"
    )
    # ceiling + workers + surge pod + the two PreSync Jobs must still fit
    assert int(quota["pods"]) >= hpa["maxReplicas"] + workers + 1 + 2
    assert any(d["kind"] == "LimitRange" for d in docs)
    assert quota["services.loadbalancers"] == "0" and quota["services.nodeports"] == "0"


def test_namespace_enforces_restricted_pod_security() -> None:
    ns = next(d for d in _build("production") if d["kind"] == "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


# ---- generated alert rule -----------------------------------------------------------------


def test_prometheusrule_is_in_sync_with_alerts_yaml() -> None:
    dst = K8S / "base" / "prometheusrule.yaml"
    assert dst.read_text() == render(), "run: python -m scripts.gen_prometheusrule"
    groups = yaml.safe_load(dst.read_text())["spec"]["groups"]
    assert sum(len(g["rules"]) for g in groups) == 13


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_web_app_is_isolated_and_routed_by_the_ingress(overlay: str) -> None:
    docs = _build(overlay)
    web = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "ekc-web")
    spec = web["spec"]["template"]["spec"]
    c = spec["containers"][0]
    assert "envFrom" not in c and "env" not in c, "the static app gets no config or secrets"
    assert not any("secret" in v for v in spec["volumes"]), "no secret volumes"
    assert spec["automountServiceAccountToken"] is False
    assert c["readinessProbe"]["httpGet"]["path"] != c["livenessProbe"]["httpGet"]["path"]
    ing = next(d for d in docs if d["kind"] == "Ingress")["spec"]["rules"][0]["http"]["paths"]
    by_path = {p["path"]: p["backend"]["service"]["name"] for p in ing}
    assert by_path == {"/api": "ekc-api", "/": "ekc-web"}
    pols = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}
    web_in = pols["allow-web-ingress"]
    assert web_in["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "ekc-web"}
    assert [p["port"] for p in web_in["ingress"][0]["ports"]] == [8080]
    # the web image is tagged separately from the API image
    assert next(i for i in [c["image"]] if i).split("/")[-1].startswith("ekc-web")
