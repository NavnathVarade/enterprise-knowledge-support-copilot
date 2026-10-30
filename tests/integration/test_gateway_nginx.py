"""The Compose gateway config, run for real: nginx in front of a real uvicorn process.

Skipped when the `nginx` binary is not installed (CI installs it). The config under test is the
actual file used by the Compose stack; only the upstream address, listen address and absolute
include path are substituted so it can run unprivileged on a developer machine.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

import redis

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "infrastructure" / "docker" / "gateway"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx binary not installed"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str, timeout: float = 40.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(url, timeout=2).status_code == 200:
                return
        time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def stack() -> Iterator[dict[str, str]]:
    # Not pytest's tmp dir (mode 700): nginx workers may run as another user (when the tests
    # run as root) and must be able to write the body/proxy temp directories.
    tmp = Path(tempfile.mkdtemp(prefix="ekc-gw-"))
    tmp.chmod(0o755)
    api_port, gw_port = _free_port(), _free_port()
    conf = (GATEWAY / "nginx.conf").read_text()
    conf = conf.replace("/etc/nginx/ekc_proxy.conf", str(tmp / "ekc_proxy.conf"))
    conf = conf.replace("server api:8000", f"server 127.0.0.1:{api_port}")
    web_port = _free_port()
    conf = conf.replace("server web:8080", f"server 127.0.0.1:{web_port}")
    conf = conf.replace("listen 8080", f"listen 127.0.0.1:{gw_port}")
    conf = conf.replace("/tmp/nginx.pid", str(tmp / "nginx.pid"))
    conf = re.sub(r"/tmp/(\w+_temp)", lambda m: str(tmp / m.group(1)), conf)
    (tmp / "nginx.conf").write_text(conf)
    shutil.copy(GATEWAY / "ekc_proxy.conf", tmp / "ekc_proxy.conf")

    env = {
        **os.environ,
        "HARDENING_TRUSTED_PROXY_HOPS": "1",
        "HARDENING_LOGIN_MAX_FAILURES_PER_IP": "100000",
        "OTEL_METRICS_BEARER_TOKEN": "gateway-test-token",
    }
    api = subprocess.Popen(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "apps.api.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
            "--no-access-log",
            "--no-proxy-headers",
        ],
        env=env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    web_dir = tmp / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text("<title>spa-behind-gateway</title>")
    web = subprocess.Popen(  # noqa: S603  # nosec B603  # stand-in for the web container
        [
            sys.executable,
            "-m",
            "http.server",
            str(web_port),
            "--bind",
            "127.0.0.1",
            "-d",
            str(web_dir),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    nginx = subprocess.Popen(  # noqa: S603  # nosec B603
        ["nginx", "-c", str(tmp / "nginx.conf")],  # noqa: S607  # nosec B607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait(f"http://127.0.0.1:{api_port}/api/v1/health/live")
        _wait(f"http://127.0.0.1:{gw_port}/healthz")
        yield {"gw": f"http://127.0.0.1:{gw_port}", "api": f"http://127.0.0.1:{api_port}"}
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603  # nosec B603
                ["nginx", "-c", str(tmp / "nginx.conf"), "-s", "stop"],  # noqa: S607  # nosec B607
                check=False,
                capture_output=True,
            )
        nginx.terminate()
        api.terminate()
        web.terminate()
        for p in (nginx, api, web):
            with contextlib.suppress(Exception):
                p.wait(timeout=10)
            p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def test_gateway_starts_with_the_shipped_config(stack: dict[str, str]) -> None:
    assert httpx.get(f"{stack['gw']}/healthz").text.strip() == "ok"


def test_smoke_script_passes_against_the_gateway(stack: dict[str, str]) -> None:
    """(Must run before the burst test, which exhausts the edge login limit.) The post-deploy smoke test, run for real against nginx + uvicorn (non-production mode)."""
    from scripts.smoke_test import run

    results = run(stack["gw"], production=False)
    failed = [r for r in results if not r.ok]
    assert not failed, failed
    assert {r.name for r in results} == {"live", "ready", "headers", "metrics", "login", "docs"}


def test_root_goes_to_the_web_app_and_api_paths_to_the_api(stack: dict[str, str]) -> None:
    root = httpx.get(f"{stack['gw']}/")
    assert root.status_code == 200 and "spa-behind-gateway" in root.text
    live = httpx.get(f"{stack['gw']}/api/v1/health/live")
    assert live.status_code == 200 and live.json() == {"status": "alive"}
    assert "spa-behind-gateway" not in live.text


def test_metrics_never_served_through_the_gateway(stack: dict[str, str]) -> None:
    auth = {"Authorization": "Bearer gateway-test-token"}
    assert httpx.get(f"{stack['api']}/metrics", headers=auth).status_code == 200  # direct
    assert httpx.get(f"{stack['gw']}/metrics", headers=auth).status_code == 404
    assert httpx.get(f"{stack['gw']}/metrics/anything", headers=auth).status_code == 404


def test_client_cannot_spoof_its_address_through_the_gateway(stack: dict[str, str]) -> None:
    """With one trusted hop the API must see the address nginx observed, not the forged one."""
    from packages.config.settings import get_settings

    r = redis.Redis.from_url(str(get_settings().redis.url), decode_responses=True)
    spoof = f"203.0.113.{uuid.uuid4().int % 250 + 1}"
    r.delete(f"login:fail:ip:{spoof}")
    before = r.get("login:fail:ip:127.0.0.1")
    resp = httpx.post(
        f"{stack['gw']}/api/v1/auth/login",
        json={
            "tenant_slug": "no-such-tenant",
            "email": f"spoof{uuid.uuid4().hex[:6]}@example.com",
            "password": "x",
        },
        headers={"X-Forwarded-For": spoof},
    )
    assert resp.status_code == 401
    assert r.get(f"login:fail:ip:{spoof}") is None  # the forged address was never used
    assert r.get("login:fail:ip:127.0.0.1") != before  # the address nginx saw was


def test_request_id_is_minted_or_preserved(stack: dict[str, str]) -> None:
    minted = httpx.get(f"{stack['gw']}/api/v1/health/live").headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", minted)
    kept = httpx.get(
        f"{stack['gw']}/api/v1/health/live", headers={"X-Request-Id": "corr-123"}
    ).headers["x-request-id"]
    assert kept == "corr-123"


def test_oversized_bodies_are_refused_with_413(stack: dict[str, str]) -> None:
    big_json = httpx.post(
        f"{stack['gw']}/api/v1/auth/login",
        content=b"a" * 2_000_000,
        headers={"Content-Type": "application/json"},
    )
    assert big_json.status_code == 413, big_json.text[:200]  # the API's own limit
    huge_upload = httpx.post(
        f"{stack['gw']}/api/v1/documents",
        content=b"\0" * 28_000_000,
        headers={"Content-Type": "application/octet-stream"},
        timeout=30,
    )
    assert huge_upload.status_code == 413  # nginx's 26m cap, before reaching the API


def test_login_burst_is_rate_limited_at_the_edge(stack: dict[str, str]) -> None:
    codes = [
        httpx.post(
            f"{stack['gw']}/api/v1/auth/login",
            json={"tenant_slug": "no-such-tenant", "email": f"b{i}@example.com", "password": "x"},
        ).status_code
        for i in range(70)
    ]
    assert 401 in codes
    assert 429 in codes  # nginx login_zone: 30 r/min, burst 20
