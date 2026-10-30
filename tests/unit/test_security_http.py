"""Phase 32 unit tests: header/size/id hygiene, JWT algorithm safety, source hygiene."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from packages.auth.jwt import InvalidToken, create_access_token, verify_access_token
from packages.config.settings import Environment, HardeningSettings, SecuritySettings, Settings
from packages.security.http import (
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
    client_ip,
    sanitize_request_id,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- request ids / client ip


def test_sanitize_request_id_keeps_boring_ids_and_replaces_the_rest() -> None:
    assert sanitize_request_id("abc-123_DEF.456:7") == "abc-123_DEF.456:7"
    for bad in (None, "", "x" * 65, "has space", "line\nbreak", 'quote"d', "ünïcode", "a/b"):
        out = sanitize_request_id(bad)
        assert uuid.UUID(out)  # a fresh uuid4


def _scope(peer: str = "10.0.0.9", xff: str | None = None) -> dict[str, Any]:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return {"type": "http", "client": (peer, 5555), "headers": headers}


def test_client_ip_ignores_forwarded_for_unless_proxies_are_trusted() -> None:
    assert client_ip(_scope(xff="6.6.6.6"), 0) == "10.0.0.9"  # spoof attempt ignored
    assert client_ip(_scope(), 0) == "10.0.0.9"


def test_client_ip_takes_the_nth_entry_from_the_right() -> None:
    # client-supplied "6.6.6.6" is leftmost; only entries added by OUR proxies count
    assert client_ip(_scope(xff="6.6.6.6, 203.0.113.7"), 1) == "203.0.113.7"
    assert client_ip(_scope(xff="6.6.6.6, 203.0.113.7, 10.1.1.1"), 2) == "203.0.113.7"
    assert client_ip(_scope(xff="203.0.113.7"), 2) == "10.0.0.9"  # chain shorter than hops


# ---------------------------------------------------------------- body limit


async def _call(app: Any, scope: dict[str, Any], chunks: list[bytes]) -> tuple[int, bytes, bool]:
    """Drive an ASGI app. Returns (status, body, app_read_all_input)."""
    sent: list[dict[str, Any]] = []
    queue = list(chunks)
    consumed = 0

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        if queue:
            consumed += 1
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, consumed == len(chunks)


class _Reader:
    """An app that reads the whole request body, like a route handler would."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        total = 0
        while True:
            m = await receive()
            total += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(total).encode()})


def _limit_app(limit: int) -> Any:
    return BodyLimitMiddleware(_Reader())


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import packages.security.http as mod

    base = mod.get_settings()
    patched = base.model_copy(
        update={"hardening": base.hardening.model_copy(update={"max_request_body_bytes": 100})}
    )
    monkeypatch.setattr(mod, "_settings", lambda scope: patched)


def _http(path: str = "/x", headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": path, "headers": headers or []}


async def test_declared_oversize_is_refused_without_reading_the_body(small_limit: None) -> None:
    status, body, read_all = await _call(
        _limit_app(100), _http(headers=[(b"content-length", b"101")]), [b"x" * 101]
    )
    assert status == 413 and json.loads(body)["error"]["code"] == "REQUEST_TOO_LARGE"
    assert read_all is False  # the application never saw it


async def test_streamed_oversize_without_content_length_is_stopped(small_limit: None) -> None:
    status, _, _ = await _call(_limit_app(100), _http(), [b"x" * 60, b"x" * 60])
    assert status == 413


async def test_body_at_the_limit_passes(small_limit: None) -> None:
    status, body, _ = await _call(_limit_app(100), _http(), [b"x" * 100])
    assert (status, body) == (200, b"100")


async def test_malformed_content_length_is_refused(small_limit: None) -> None:
    status, _, _ = await _call(
        _limit_app(100), _http(headers=[(b"content-length", b"abc")]), [b"x"]
    )
    assert status == 413


async def test_upload_path_gets_the_larger_upload_limit(small_limit: None) -> None:
    # 150 bytes > the 100-byte default, but the upload route allows max_upload + overhead
    status, _, _ = await _call(
        _limit_app(100),
        _http("/api/v1/documents", [(b"content-length", b"150")]),
        [b"x" * 150],
    )
    assert status == 200


# ---------------------------------------------------------------- security headers


class _Hello:
    def __init__(self, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        self.extra = extra or []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": list(self.extra)})
        await send({"type": "http.response.body", "body": b"{}"})


async def _headers_for(
    path: str, env: Environment, extra: list[tuple[bytes, bytes]] | None = None
) -> dict[str, str]:
    import packages.security.http as mod

    base = mod.get_settings()
    patched = base.model_copy(update={"environment": env})
    original = mod._settings
    mod._settings = lambda scope: patched  # type: ignore[assignment]
    try:
        sent: list[dict[str, Any]] = []

        async def send(m: dict[str, Any]) -> None:
            sent.append(m)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b""}

        await SecurityHeadersMiddleware(_Hello(extra))(
            {"type": "http", "path": path, "headers": []}, receive, send
        )
    finally:
        mod._settings = original  # type: ignore[assignment]
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.decode(): v.decode() for k, v in start["headers"]}


async def test_baseline_headers_on_every_response() -> None:
    h = await _headers_for("/api/v1/x", Environment.test)
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "strict-transport-security" not in h  # not over plain HTTP in dev/test


async def test_hsts_only_in_staging_and_production() -> None:
    for env in (Environment.staging, Environment.production):
        assert "max-age=" in (await _headers_for("/x", env))["strict-transport-security"]


async def test_handler_set_headers_are_not_overwritten() -> None:
    h = await _headers_for("/x", Environment.test, [(b"cache-control", b"max-age=60")])
    assert h["cache-control"] == "max-age=60"


async def test_csp_is_skipped_for_the_dev_docs_pages_only() -> None:
    assert "content-security-policy" not in await _headers_for("/docs", Environment.test)
    assert "content-security-policy" in await _headers_for("/docs-evil", Environment.test)


# ---------------------------------------------------------------- JWT safety


def _pem() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


@pytest.fixture(scope="module")
def sec() -> SecuritySettings:
    priv, pub = _pem()
    return SecuritySettings(
        jwt_public_key=pub, jwt_private_key=priv, jwt_issuer="iss", jwt_audience="aud"
    )


def _claims(sec: SecuritySettings, **over: Any) -> dict[str, Any]:
    now = int(time.time())
    c: dict[str, Any] = {
        "sub": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()), "email": "a@b.co",
        "roles": ["admin"], "groups": [], "iss": sec.jwt_issuer, "aud": sec.jwt_audience,
        "iat": now, "exp": now + 600,
    }  # fmt: skip
    c.update(over)
    return c


def test_valid_token_roundtrips(sec: SecuritySettings) -> None:
    tok = create_access_token(
        settings=sec, user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), email="a@b.co",
        roles=frozenset({"admin"}), groups=frozenset(),
    )  # fmt: skip
    assert verify_access_token(tok, settings=sec).roles == frozenset({"admin"})


def test_alg_none_is_rejected(sec: SecuritySettings) -> None:
    tok = pyjwt.encode(_claims(sec), key="", algorithm="none")
    with pytest.raises(InvalidToken):
        verify_access_token(tok, settings=sec)


def test_hs256_signed_with_the_public_key_is_rejected(sec: SecuritySettings) -> None:
    """Classic algorithm-confusion forgery: sign HS256 using the (public) verification key."""
    import base64
    import hashlib
    import hmac

    def b64(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(_claims(sec)).encode())
    sig = b64(hmac.new(sec.jwt_public_key.encode(), head + b"." + body, hashlib.sha256).digest())
    with pytest.raises(InvalidToken):
        verify_access_token((head + b"." + body + b"." + sig).decode(), settings=sec)


@pytest.mark.parametrize("missing", ["exp", "iat", "iss", "aud", "sub"])
def test_tokens_missing_required_claims_are_rejected(sec: SecuritySettings, missing: str) -> None:
    claims = _claims(sec)
    del claims[missing]
    tok = pyjwt.encode(claims, sec.jwt_private_key, algorithm="RS256")
    with pytest.raises(InvalidToken):
        verify_access_token(tok, settings=sec)


def test_expired_and_wrong_issuer_are_rejected(sec: SecuritySettings) -> None:
    old = pyjwt.encode(_claims(sec, exp=int(time.time()) - 3600), sec.jwt_private_key, "RS256")
    wrong_iss = pyjwt.encode(_claims(sec, iss="evil"), sec.jwt_private_key, "RS256")
    for tok in (old, wrong_iss):
        with pytest.raises(InvalidToken):
            verify_access_token(tok, settings=sec)


@pytest.mark.parametrize("alg", ["HS256", "HS512", "none", "NONE", ""])
def test_symmetric_and_none_algorithms_cannot_be_configured(alg: str) -> None:
    with pytest.raises(ValidationError):
        SecuritySettings(jwt_public_key="k", jwt_issuer="i", jwt_audience="a", jwt_algorithm=alg)


def test_hardening_defaults_are_the_safe_choice() -> None:
    h = HardeningSettings()
    assert h.docs_enabled is None and h.trusted_proxy_hops == 0 and h.login_throttle_fail_open
    assert Settings.model_fields["hardening"].annotation is HardeningSettings


# ---------------------------------------------------------------- source hygiene

_HIDDEN = re.compile(
    "["
    + "".join(
        chr(c)
        for r in ((0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069))
        for c in range(r[0], r[1] + 1)
    )
    + chr(0xFEFF)
    + "]"
)


def test_no_hidden_or_bidirectional_characters_in_source() -> None:
    """'Trojan source' guard: such characters may only appear as ESCAPES, never literally."""
    offenders = []
    for base in ("packages", "apps", "rag", "scripts", "data", "tests", "migrations"):
        for path in (ROOT / base).rglob("*.py"):
            if _HIDDEN.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_production_code_has_no_bare_assert() -> None:
    """`assert` disappears under `python -O`; it must not be used for validation."""
    offenders = []
    for base in ("packages", "apps", "rag"):
        for path in (ROOT / base).rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"\s*assert\b", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert offenders == []
