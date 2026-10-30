"""Brings up the whole stack for browser tests (module docstring of harness.py lists the stand-ins).

    browser -> gateway nginx (real infrastructure/docker/gateway/nginx.conf)
                 |- /api/* -> uvicorn running the real API (tests.e2e.harness)
                 `- /      -> web nginx (real apps/web/nginx/default.conf) serving the real build

Skipped (not failed) when nginx, Chromium or a built/buildable frontend is unavailable.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "apps" / "web"


@dataclass
class Stack:
    base: str  # the single public origin (the gateway)
    users: dict[str, Any]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(url: str, timeout: float = 60.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(url, timeout=2).status_code == 200:
                return
        time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


def _ensure_frontend_build() -> Path:
    dist = WEB / "dist"
    if (dist / "index.html").is_file():
        return dist
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("apps/web is not built and npm is not installed")
    for args in (["ci", "--no-audit", "--no-fund"], ["run", "build"]):
        subprocess.run([npm, *args], cwd=WEB, check=True, capture_output=True)  # noqa: S603  # nosec B603
    return dist


# Module scope on purpose: the harness runs an outbox relay and an ingestion consumer against the
# test database; they must be stopped before any other test module creates outbox rows there.
@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    if shutil.which("nginx") is None:
        pytest.skip("nginx binary not installed")
    dist = _ensure_frontend_build()
    tmp = Path(tempfile.mkdtemp(prefix="ekc-e2e-"))
    tmp.chmod(0o755)
    api_port, web_port, gw_port = _free_port(), _free_port(), _free_port()

    # --- web nginx: the shipped site config, serving a copy of the real build
    html = tmp / "html"
    shutil.copytree(dist, html)
    for p in [html, *html.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    shutil.copy(WEB / "nginx" / "security_headers.conf", tmp / "headers.conf")
    site = (WEB / "nginx" / "default.conf").read_text()
    site = site.replace("/etc/nginx/ekc_security_headers.conf", str(tmp / "headers.conf"))
    site = site.replace("root /usr/share/nginx/html;", f"root {html};")
    site = site.replace("listen 8080;", f"listen 127.0.0.1:{web_port};")
    (tmp / "site.conf").write_text(site)
    (tmp / "web.conf").write_text(
        f"""
pid {tmp}/web.pid;
error_log {tmp}/web-error.log warn;
events {{ worker_connections 64; }}
http {{
    include /etc/nginx/mime.types;
    access_log off;
    client_body_temp_path {tmp}/wc; proxy_temp_path {tmp}/wp; fastcgi_temp_path {tmp}/wf;
    uwsgi_temp_path {tmp}/wu; scgi_temp_path {tmp}/ws;
    include {tmp}/site.conf;
}}
"""
    )

    # --- gateway nginx: the shipped config, upstreams pointed at the local processes
    gw = (ROOT / "infrastructure/docker/gateway/nginx.conf").read_text()
    gw = gw.replace("/etc/nginx/ekc_proxy.conf", str(tmp / "ekc_proxy.conf"))
    gw = gw.replace("server api:8000", f"server 127.0.0.1:{api_port}")
    gw = gw.replace("server web:8080", f"server 127.0.0.1:{web_port}")
    gw = gw.replace("listen 8080", f"listen 127.0.0.1:{gw_port}")
    gw = gw.replace("/tmp/nginx.pid", str(tmp / "gw.pid"))
    gw = re.sub(r"/tmp/(\w+_temp)", lambda m: str(tmp / ("gw_" + m.group(1))), gw)
    (tmp / "gw.conf").write_text(gw)
    shutil.copy(ROOT / "infrastructure/docker/gateway/ekc_proxy.conf", tmp / "ekc_proxy.conf")

    state_file = tmp / "state.json"
    env = {
        **os.environ,
        "HARDENING_TRUSTED_PROXY_HOPS": "1",
        "HARDENING_LOGIN_MAX_FAILURES_PER_IP": "100000",
        "HARDENING_LOGIN_MAX_FAILURES_PER_ACCOUNT": "100",
    }
    api = subprocess.Popen(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-m",
            "tests.e2e.harness",
            "--port",
            str(api_port),
            "--state-file",
            str(state_file),
        ],
        env=env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    procs = [
        subprocess.Popen(  # noqa: S603  # nosec B603
            ["nginx", "-c", str(tmp / name)],  # noqa: S607  # nosec B607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for name in ("web.conf", "gw.conf")
    ]
    try:
        _wait(f"http://127.0.0.1:{api_port}/api/v1/health/ready")
        _wait(f"http://127.0.0.1:{gw_port}/healthz")
        _wait(f"http://127.0.0.1:{web_port}/healthz")
        yield Stack(base=f"http://127.0.0.1:{gw_port}", users=json.loads(state_file.read_text()))
    finally:
        for name in ("web.conf", "gw.conf"):
            subprocess.run(  # noqa: S603  # nosec B603
                ["nginx", "-c", str(tmp / name), "-s", "stop"],  # noqa: S607  # nosec B607
                check=False,
                capture_output=True,
            )
        for p in (*procs, api):
            p.terminate()
        for p in (*procs, api):
            with contextlib.suppress(Exception):
                p.wait(timeout=15)
            p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


# Module scope on purpose: the sync Playwright API keeps an event loop running in this thread until it
# stops, which would break pytest-asyncio tests that run after the e2e module.
@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            # EKC_CHROMIUM_PATH: use an already-installed Chromium (offline machines, or a browser build
            # that does not match the locked Playwright version). Otherwise Playwright's own download
            # (`uv run playwright install chromium`) is used.
            b = pw.chromium.launch(
                args=["--no-sandbox"], executable_path=os.environ.get("EKC_CHROMIUM_PATH") or None
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium is not available: {type(exc).__name__}")
        yield b
        b.close()
