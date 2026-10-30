"""Policy tests for the Docker Compose reference environment (Phase 35).

Static checks over the real files: they fail if someone un-pins an image, publishes a database
port, drops a hardening flag, or lets the gateway and the proxy-hop setting drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "infrastructure" / "docker"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((DOCKER / name).read_text())  # type: ignore[no-any-return]


BASE = _load("docker-compose.yml")
OBS = _load("docker-compose.observability.yml")
SERVICES: dict[str, Any] = {**BASE["services"], **OBS["services"]}
APP_SERVICES = ["migrate", "topics-init", "api", "ingestion-worker", "outbox-relay"]
LONG_RUNNING = [
    "postgres", "redis", "kafka", "minio", "api", "ingestion-worker", "outbox-relay", "gateway",
    "prometheus", "grafana",
]  # fmt: skip


def test_every_third_party_image_is_pinned_to_an_exact_tag() -> None:
    for name, svc in SERVICES.items():
        image = svc.get("image")
        if image is None or image == "ekc:compose" or "build" in svc:
            continue  # first-party images are built from this repo, not pulled
        assert ":" in image, f"{name}: image has no tag"
        tag = image.rsplit(":", 1)[1]
        assert tag != "latest", f"{name}: floating tag"
        assert re.search(r"\d", tag), f"{name}: tag {tag!r} does not look like a version"


def test_only_the_gateway_and_observability_publish_ports_and_only_on_loopback_by_default() -> None:
    published = {n for n, s in SERVICES.items() if s.get("ports")}
    assert published == {"gateway", "prometheus", "grafana"}
    for n in published:
        for p in SERVICES[n]["ports"]:
            assert re.match(r"^\$\{\w+:-127\.0\.0\.1\}:", p), f"{n}: {p} is not loopback by default"
    for n in ("postgres", "redis", "kafka", "minio", "api"):
        assert not SERVICES[n].get("ports"), f"{n} must not publish a host port"


@pytest.mark.parametrize("name", APP_SERVICES)
def test_application_containers_are_hardened(name: str) -> None:
    s = SERVICES[name]
    assert s["read_only"] is True
    assert "/tmp" in s["tmpfs"]
    assert s["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in s["security_opt"]
    assert s["image"] == "ekc:compose" or s.get("build")


@pytest.mark.parametrize("name", ["gateway", "prometheus", "grafana"])
def test_edge_and_observability_containers_drop_capabilities(name: str) -> None:
    s = SERVICES[name]
    assert s["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in s["security_opt"]


@pytest.mark.parametrize("name", LONG_RUNNING)
def test_long_running_services_have_resource_limits_and_bounded_logs(name: str) -> None:
    s = SERVICES[name]
    limits = s["deploy"]["resources"]["limits"]
    assert {"cpus", "memory", "pids"} <= set(limits)
    assert s["logging"]["options"]["max-size"]
    assert s["logging"]["options"]["max-file"]


def test_data_network_is_internal_and_only_app_services_straddle_both_networks() -> None:
    assert BASE["networks"]["data-net"]["internal"] is True
    assert not BASE["networks"]["app-net"].get("internal")
    for n in ("postgres", "redis", "kafka", "minio", "minio-init", "db-roles"):
        assert SERVICES[n]["networks"] == ["data-net"], n
    assert SERVICES["gateway"]["networks"] == ["app-net"]
    for n in ("api", "ingestion-worker"):  # need providers (internet) AND the data services
        assert set(SERVICES[n]["networks"]) == {"app-net", "data-net"}, n


def test_every_scrape_target_shares_a_network_with_prometheus() -> None:
    assert BASE["networks"]["metrics-net"]["internal"] is True
    prom_nets = set(SERVICES["prometheus"]["networks"])
    prom = yaml.safe_load((DOCKER / "prometheus" / "prometheus.yml").read_text())
    for job in prom["scrape_configs"]:
        for sc in job["static_configs"]:
            for target in sc["targets"]:
                svc = target.split(":")[0]
                assert prom_nets & set(SERVICES[svc]["networks"]), f"prometheus cannot reach {svc}"


def test_no_localhost_between_containers() -> None:
    allowed = re.compile(r"127\.0\.0\.1:(8000|8080)/")  # a container probing its OWN health
    for name, s in SERVICES.items():
        for value in (s.get("environment") or {}).values():
            assert "localhost" not in str(value) and "127.0.0.1" not in str(value), name
        for hc in (s.get("healthcheck", {}).get("test") or [],):
            joined = " ".join(map(str, hc))
            if "127.0.0.1" in joined or "localhost" in joined:
                assert allowed.search(joined), f"{name}: {joined}"
        cmd = " ".join(map(str, s.get("command", []) or []))
        assert "localhost" not in cmd, name


def test_secrets_have_no_defaults_and_nothing_is_hardcoded() -> None:
    text = (DOCKER / "docker-compose.yml").read_text()
    for var in (
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_PASSWORD",
        "ANALYTICS_RO_PASSWORD",
        "METRICS_TOKEN",
    ):
        assert f"${{{var}:?" in text, f"{var} must be a required variable"
        assert not re.search(rf"\$\{{{var}:-", text), f"{var} must not have a default"
    obs = (DOCKER / "docker-compose.observability.yml").read_text()
    assert "${GRAFANA_ADMIN_PASSWORD:?" in obs
    for src in (text, obs):
        for m in re.finditer(
            r"(?im)^\s*[A-Za-z_]*(?:password|secret|token)[A-Za-z_]*:[ \t]*(\S+)", src
        ):
            value = m.group(1)
            assert value.startswith(("$", "#", "[", "{")), m.group(0)


def test_example_env_contains_only_placeholders() -> None:
    for line in (ROOT / ".env.compose.example").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            assert line.split("=", 1)[1] in {"__SECRET__"}, line


def test_startup_order_is_migrate_then_roles_then_services() -> None:
    assert "migrate" in SERVICES["db-roles"]["depends_on"]
    for n in ("api", "ingestion-worker", "outbox-relay"):
        assert "db-roles" in SERVICES[n]["depends_on"], n
    assert "minio-init" in SERVICES["api"]["depends_on"]
    assert "minio-init" in SERVICES["ingestion-worker"]["depends_on"]
    assert SERVICES["gateway"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert "ekc-documents" in SERVICES["minio-init"]["entrypoint"][-1]
    assert SERVICES["api"]["environment"]["OBJECT_STORE_BUCKET"] == "ekc-documents"


def test_stop_grace_exceeds_the_servers_graceful_shutdown_budget() -> None:
    dockerfile = (DOCKER / "Dockerfile").read_text()
    budget = int(re.search(r'"--timeout-graceful-shutdown", "(\d+)"', dockerfile).group(1))  # type: ignore[union-attr]
    grace = int(str(SERVICES["api"]["stop_grace_period"]).rstrip("s"))
    assert grace > budget
    assert "STOPSIGNAL SIGTERM" in dockerfile


def test_gateway_and_application_agree_about_the_trusted_proxy_chain() -> None:
    env = BASE["services"]["api"]["environment"]
    assert env["HARDENING_TRUSTED_PROXY_HOPS"] == "1"
    conf = (DOCKER / "gateway" / "nginx.conf").read_text()
    proxy = (DOCKER / "gateway" / "ekc_proxy.conf").read_text()
    # Exactly one proxy appends to the chain, and it APPENDS (never replaces with a client value).
    assert "$proxy_add_x_forwarded_for" in proxy
    assert "proxy_next_upstream off" in proxy
    assert re.search(r"location /metrics\s*\{\s*return 404;", conf)
    assert "client_max_body_size  26m" in conf  # >= 25 MiB upload cap + overhead
    assert "server api:8000" in conf  # service DNS, not localhost
    assert "127.0.0.1" not in conf.replace("# ", "") or "healthz" in conf


def test_prometheus_targets_are_compose_services_and_the_token_is_a_secret() -> None:
    prom = yaml.safe_load((DOCKER / "prometheus" / "prometheus.yml").read_text())
    for job in prom["scrape_configs"]:
        assert job["authorization"]["credentials_file"] == "/run/secrets/metrics_token"
        for sc in job["static_configs"]:
            for target in sc["targets"]:
                assert target.split(":")[0] in BASE["services"], target
    assert OBS["secrets"]["metrics_token"] == {"environment": "METRICS_TOKEN"}
    assert "metrics_token" in SERVICES["prometheus"]["secrets"]
    mounted = [v.split(":")[0] for v in SERVICES["prometheus"]["volumes"] if v.startswith("../")]
    for rel in mounted:
        assert (DOCKER / rel).exists(), rel


def test_grafana_dashboard_and_provisioning_files_exist() -> None:
    for rel in (
        "grafana/provisioning/datasources/prometheus.yml",
        "grafana/provisioning/dashboards/dashboards.yml",
    ):
        assert yaml.safe_load((DOCKER / rel).read_text())["apiVersion"] == 1
    assert (ROOT / "monitoring" / "grafana" / "ekc-overview.json").exists()
    assert SERVICES["grafana"]["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"


def test_web_service_is_hardened_unpublished_and_fronted_by_the_gateway() -> None:
    web = SERVICES["web"]
    assert web["build"]["context"] == "../../apps/web"
    assert web["read_only"] is True and web["cap_drop"] == ["ALL"]
    assert "ports" not in web, "only the gateway publishes a port"
    assert "env_file" not in web and "environment" not in web, "the static app gets no config"
    assert web["networks"] == ["app-net"]
    assert "web" in SERVICES["gateway"]["depends_on"]
