"""Phase 44 audit: every route the API exposes demands a valid bearer token unless it is on a short,
reviewed allowlist. A new endpoint added without auth fails here, not in production."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# (method, path) pairs that are deliberately reachable without a token.
PUBLIC = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/signup"),
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
}
# Served only on the internal listener or hidden in production; reviewed individually.
NOT_APP_ROUTES = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/metrics")

pytestmark = pytest.mark.security


def _all_routes(app: FastAPI) -> set[tuple[str, str]]:
    """Union of the OpenAPI schema (includes routers mounted indirectly) and the router table."""
    found = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}
    for r in app.routes:
        path, methods = getattr(r, "path", None), getattr(r, "methods", None)
        if path and methods:
            found |= {(m, path) for m in methods if m not in {"HEAD", "OPTIONS"}}
    return found


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)


async def test_every_non_public_route_rejects_a_missing_or_garbage_token() -> None:
    from apps.api.app.main import create_app

    app = create_app()
    checked: list[tuple[str, str]] = []
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        for method, path in sorted(_all_routes(app)):
            if path in NOT_APP_ROUTES or (method, path) in PUBLIC:
                continue
            for headers in ({}, {"Authorization": "Bearer not-a-jwt"}):
                resp = await client.request(method, _fill(path), headers=headers)
                assert resp.status_code == 401, (
                    f"{method} {path} answered {resp.status_code} without valid credentials"
                )
            checked.append((method, path))
    assert len(checked) >= 8, f"suspiciously few routes audited: {checked}"


async def test_allowlisted_public_routes_still_exist() -> None:
    """A stale allowlist hides drift; every entry must correspond to a real route."""
    from apps.api.app.main import create_app

    app = create_app()
    assert _all_routes(app) >= PUBLIC, PUBLIC - _all_routes(app)
