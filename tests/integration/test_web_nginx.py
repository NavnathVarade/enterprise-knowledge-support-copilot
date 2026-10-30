"""The frontend's nginx config, run for real (skipped when nginx is not installed).

The shipped default.conf and security_headers.conf are used verbatim; only the listen port, the
document root and the absolute include path are substituted so it runs unprivileged on a dev box.
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "apps" / "web" / "nginx"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx binary not installed"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def web() -> Iterator[str]:
    tmp = Path(tempfile.mkdtemp(prefix="ekc-web-"))
    tmp.chmod(0o755)
    port = _free_port()
    html = tmp / "html"
    (html / "assets").mkdir(parents=True)
    (html / "index.html").write_text("<!doctype html><title>spa</title>")
    (html / "assets" / "index-abc123.js").write_text("console.log(1)")
    (html / "assets" / "index-abc123.js.map").write_text("{}")
    (html / ".secret").write_text("nope")
    shutil.copy(WEB / "security_headers.conf", tmp / "headers.conf")
    site = (WEB / "default.conf").read_text()
    site = site.replace("/etc/nginx/ekc_security_headers.conf", str(tmp / "headers.conf"))
    site = site.replace("root /usr/share/nginx/html;", f"root {html};")
    site = site.replace("listen 8080;", f"listen 127.0.0.1:{port};")
    (tmp / "site.conf").write_text(site)
    (tmp / "nginx.conf").write_text(
        f"""
pid {tmp}/nginx.pid;
error_log {tmp}/error.log warn;
events {{ worker_connections 64; }}
http {{
    include /etc/nginx/mime.types;
    access_log off;
    client_body_temp_path {tmp}/c; proxy_temp_path {tmp}/p; fastcgi_temp_path {tmp}/f;
    uwsgi_temp_path {tmp}/u; scgi_temp_path {tmp}/s;
    include {tmp}/site.conf;
}}
"""
    )
    proc = subprocess.Popen(  # noqa: S603  # nosec B603
        ["nginx", "-c", str(tmp / "nginx.conf")],  # noqa: S607  # nosec B607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        end = time.monotonic() + 15
        while time.monotonic() < end:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    break
            time.sleep(0.2)
        else:
            raise RuntimeError("web nginx did not start")
        yield base
    finally:
        subprocess.run(  # noqa: S603  # nosec B603
            ["nginx", "-c", str(tmp / "nginx.conf"), "-s", "stop"],  # noqa: S607  # nosec B607
            check=False,
            capture_output=True,
        )
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


def test_app_shell_is_served_with_the_strict_csp_and_revalidates(web: str) -> None:
    r = httpx.get(f"{web}/")
    assert r.status_code == 200 and "<title>spa</title>" in r.text
    csp = r.headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "connect-src 'self'",
    ):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "http:" not in csp
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    assert "nginx/" not in r.headers.get("server", "")


def test_client_side_routes_fall_back_to_the_shell(web: str) -> None:
    for path in ("/documents", "/usage", "/some/deep/link"):
        r = httpx.get(f"{web}{path}")
        assert r.status_code == 200 and "<title>spa</title>" in r.text
        assert "content-security-policy" in r.headers


def test_hashed_assets_are_cached_forever_and_keep_the_csp(web: str) -> None:
    r = httpx.get(f"{web}/assets/index-abc123.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "content-security-policy" in r.headers
    assert httpx.get(f"{web}/assets/missing.js").status_code == 404


def test_api_paths_are_never_answered_with_the_shell(web: str) -> None:
    for path in ("/api/v1/documents", "/api", "/metrics", "/metrics/x"):
        r = httpx.get(f"{web}{path}")
        assert r.status_code == 404, path
        assert "<title>spa</title>" not in r.text


def test_dotfiles_and_source_maps_are_not_served(web: str) -> None:
    assert httpx.get(f"{web}/.secret").status_code == 404
    assert httpx.get(f"{web}/assets/index-abc123.js.map").status_code == 404
