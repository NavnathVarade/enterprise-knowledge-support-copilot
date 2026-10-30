"""HTTP-surface hardening: security headers, request-size limits, request-id hygiene, client IP.

All pure ASGI (no BaseHTTPMiddleware: it forks execution and can leak exceptions past the
registered handlers -- see RequestContextMiddleware's history). Settings are read lazily from
`app.state.settings` (set at startup) with a fallback to `get_settings()`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from packages.config.settings import Environment, Settings, get_settings

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
UPLOAD_PATH = "/api/v1/documents"


def _settings(scope: Scope) -> Settings:
    app = scope.get("app")
    state = getattr(app, "state", None)
    found = getattr(state, "settings", None)
    return found if isinstance(found, Settings) else get_settings()


def sanitize_request_id(raw: str | None) -> str:
    """Client-supplied correlation ids are accepted only if they are short and boring;
    anything else (control characters, huge values, log-injection payloads) is replaced."""
    if raw and _ID_RE.fullmatch(raw):
        return str(raw)
    return str(uuid.uuid4())


def client_ip(scope: Scope, trusted_proxy_hops: int) -> str:
    """The caller's address. With `trusted_proxy_hops == 0` X-Forwarded-For is ignored, so a
    client cannot choose its own identity. With N trusted hops the Nth entry from the RIGHT is
    used (entries to its left are client-controlled and never trusted)."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    if trusted_proxy_hops <= 0:
        return str(peer)
    headers = dict(scope.get("headers") or [])
    forwarded: bytes = headers.get(b"x-forwarded-for", b"")
    chain = [p.strip() for p in forwarded.decode("latin-1").split(",") if p.strip()]
    if len(chain) < trusted_proxy_hops:
        return str(peer)
    return str(chain[-trusted_proxy_hops][:64])


def _json_response(status: int, code: str, message: str) -> tuple[Message, Message]:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    start: Message = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    return start, {"type": "http.response.body", "body": body}


class BodyLimitMiddleware:
    """Rejects oversized request bodies BEFORE the application buffers them. Uses
    Content-Length when present and counts streamed bytes otherwise (chunked uploads).
    The upload endpoint gets `max upload size + multipart overhead`; everything else gets
    `HARDENING_MAX_REQUEST_BODY_BYTES`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _limit(self, scope: Scope) -> int:
        s = _settings(scope)
        if scope.get("path") == UPLOAD_PATH and scope.get("method") == "POST":
            return s.ingestion.max_upload_size_bytes + s.hardening.upload_overhead_bytes
        return s.hardening.max_request_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = True  # malformed length: refuse rather than guess
            if too_big:
                for m in _json_response(413, "REQUEST_TOO_LARGE", "Request body is too large."):
                    await send(m)
                return

        seen = 0
        started = False  # the APPLICATION began its own response
        refused = False  # WE sent the 413; whatever the application sends afterwards is dropped

        async def counting_receive() -> Message:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request" and not refused:
                seen += len(message.get("body", b""))
                if seen > limit and not started:
                    # Answer 413 ourselves. Raising instead would be swallowed by the
                    # framework's body parsing and surface as a 400.
                    refused = True
                    for m in _json_response(413, "REQUEST_TOO_LARGE", "Request body is too large."):
                        await send(m)
                    return {"type": "http.disconnect"}
                if seen > limit:
                    return {"type": "http.disconnect"}  # response already on the wire
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if refused:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, counting_receive, tracking_send)


class SecurityHeadersMiddleware:
    """Adds conservative response headers to every HTTP response. This is a JSON API: no
    framing, no sniffing, no referrer, no caching of authenticated responses, a CSP that
    allows nothing, and HSTS in staging/production only."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def _headers(self, scope: Scope) -> list[tuple[bytes, bytes]]:
        s = _settings(scope)
        path = scope.get("path", "")
        h = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (b"cross-origin-resource-policy", b"same-origin"),
            (b"cache-control", b"no-store"),
        ]
        if path not in _DOC_PATHS and not path.startswith(
            "/docs/"
        ):  # Swagger UI (dev only) needs inline script/CDN
            h.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
        if (
            s.environment in (Environment.staging, Environment.production)
            and s.hardening.hsts_max_age_seconds > 0
        ):
            h.append(
                (
                    b"strict-transport-security",
                    f"max-age={s.hardening.hsts_max_age_seconds}; includeSubDomains".encode(),
                )
            )
        return h

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = self._headers(scope)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                headers: list[Any] = list(message.get("headers", []))
                headers.extend((k, v) for k, v in extra if k not in existing)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
