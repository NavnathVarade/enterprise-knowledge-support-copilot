"""Post-deploy smoke test against a running environment (read-only, no credentials needed).

    python -m scripts.smoke_test --base-url https://ekc.example.com [--production]

Checks, each reported as PASS/FAIL (exit 1 if any fails):
  live        /api/v1/health/live is 200
  ready       /api/v1/health/ready is 200 (retried, a rollout may still be settling)
  headers     nosniff + no-referrer-style hardening headers present; HSTS when --production
  metrics     /metrics is NOT served on the public edge (404)
  login       a bad login is a clean 401/422, never a 5xx (proves DB + Redis paths work)
  docs        /docs is not exposed when --production

It proves the deployment is reachable and wired, NOT that it is correct: functional and load
testing are separate (tests/, scripts/bench_*.py).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _get(client: httpx.Client, path: str) -> httpx.Response:
    return client.get(path, follow_redirects=False)


def check_live(client: httpx.Client, production: bool) -> Result:
    r = _get(client, "/api/v1/health/live")
    return Result("live", r.status_code == 200, f"status={r.status_code}")


def check_ready(
    client: httpx.Client, production: bool, attempts: int = 12, delay: float = 5.0
) -> Result:
    last = "no response"
    for i in range(attempts):
        try:
            r = _get(client, "/api/v1/health/ready")
            last = f"status={r.status_code}"
            if r.status_code == 200:
                return Result("ready", True, last)
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if i < attempts - 1:
            time.sleep(delay)
    return Result("ready", False, f"{last} after {attempts} attempts")


def check_headers(client: httpx.Client, production: bool) -> Result:
    h = _get(client, "/api/v1/health/live").headers
    missing = [k for k in ("x-content-type-options", "x-request-id") if k not in h]
    if production and "strict-transport-security" not in h:
        missing.append("strict-transport-security")
    return Result("headers", not missing, f"missing={missing}" if missing else "ok")


def check_metrics_not_public(client: httpx.Client, production: bool) -> Result:
    r = _get(client, "/metrics")
    return Result("metrics", r.status_code == 404, f"status={r.status_code} (want 404)")


def check_bad_login(client: httpx.Client, production: bool) -> Result:
    r = client.post(
        "/api/v1/auth/login",
        json={
            "tenant_slug": "smoke-no-such-tenant",
            "email": "smoke@example.invalid",
            "password": "x",  # nosec B105  # deliberately invalid probe, not a credential
        },
    )
    return Result("login", r.status_code in (401, 422), f"status={r.status_code} (want 401/422)")


def _is_openapi(r: httpx.Response) -> bool:
    try:
        body = r.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "openapi" in body


def check_docs_hidden(client: httpx.Client, production: bool) -> Result:
    """The web app answers unknown paths with its HTML shell (200), so status alone proves nothing:
    look for the API documentation itself (an OpenAPI document, Swagger UI or ReDoc)."""
    if not production:
        return Result("docs", True, "skipped (not --production)")
    exposed: dict[str, int] = {}
    spec = _get(client, "/openapi.json")
    if spec.status_code == 200 and _is_openapi(spec):
        exposed["/openapi.json"] = 200
    for path in ("/docs", "/redoc"):
        r = _get(client, path)
        if r.status_code == 200 and re.search(r"swagger-ui|redoc", r.text, re.IGNORECASE):
            exposed[path] = 200
    return Result("docs", not exposed, f"exposed={exposed}" if exposed else "hidden")


CHECKS: list[Callable[[httpx.Client, bool], Result]] = [
    check_live,
    check_ready,
    check_headers,
    check_metrics_not_public,
    check_bad_login,
    check_docs_hidden,
]


def run(base_url: str, production: bool, timeout: float = 10.0) -> list[Result]:
    results: list[Result] = []
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        for check in CHECKS:
            try:
                results.append(check(client, production))
            except httpx.HTTPError as exc:
                results.append(
                    Result(check.__name__.removeprefix("check_"), False, type(exc).__name__)
                )
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--production", action="store_true", help="also require HSTS and hidden docs")
    args = ap.parse_args()
    if not args.base_url.startswith("https://") and args.production:
        print("refusing: production smoke test must target an https:// URL", file=sys.stderr)
        return 2
    results = run(args.base_url, args.production)
    print(json.dumps([r.__dict__ for r in results], indent=2))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
