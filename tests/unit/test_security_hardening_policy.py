"""Phase 32 policy tests: configuration and repository hygiene that must not regress."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from packages.config.settings import Settings
from packages.security.known_keys import is_committed_test_key, public_key_fingerprint
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "infrastructure" / "docker" / "Dockerfile").read_text()


def _test_env() -> dict[str, str]:
    """Parse .env.test (multi-line quoted PEM values included)."""
    text = (ROOT / ".env.test").read_text()
    out: dict[str, str] = {}
    for m in re.finditer(r'^([A-Z0-9_]+)=("([^"]*)"|[^\n]*)$', text, re.M | re.S):
        out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return out


def test_the_committed_test_key_is_recognised_and_refused_in_real_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pub = _test_env()["SECURITY_JWT_PUBLIC_KEY"]
    assert is_committed_test_key(pub)
    assert public_key_fingerprint(pub) == public_key_fingerprint(pub.replace("\n", " "))
    for k, v in {
        "ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql+asyncpg://u:p@pg.data.svc:5432/ekc?ssl=require",
        "REDIS_URL": "rediss://redis.data.svc:6379/0",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka-0.kafka.data.svc:9093",
        "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
        "KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
        "KAFKA_SASL_USERNAME": "u",
        "KAFKA_SASL_PASSWORD": "p",
        "SECURITY_JWT_PUBLIC_KEY": pub,
        "SECURITY_JWT_ISSUER": "i",
        "SECURITY_JWT_AUDIENCE": "a",
        "OBJECT_STORE_BUCKET": "b",
    }.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError, match="committed to this repository"):
        Settings()  # type: ignore[call-arg]


def test_a_freshly_generated_key_is_not_flagged() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    assert not is_committed_test_key(pem)


def test_dockerfile_never_trusts_forwarded_headers_from_any_peer() -> None:
    cmd = re.search(r"^CMD (\[.*\])$", DOCKERFILE, re.M)
    assert cmd is not None
    assert "--forwarded-allow-ips" not in cmd.group(1)
    assert "--no-proxy-headers" in cmd.group(1) and "--no-server-header" in cmd.group(1)


def test_gitleaks_allowlist_is_exactly_the_two_documented_fixtures() -> None:
    import tomllib

    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert cfg["extend"]["useDefault"] is True
    assert sorted(cfg["allowlist"]["paths"]) == sorted(
        [r"^\.env\.test$", r"^data/synthetic/generator\.py$"]
    )


@pytest.mark.parametrize("name", [".env.example", ".env.production.example"])
def test_env_example_contains_only_placeholders(name: str) -> None:
    text = (ROOT / name).read_text()
    assert "BEGIN" not in text.replace("__PEM", "")  # no real PEM block
    for line in text.splitlines():
        m = re.match(r"^([A-Z_]*(?:SECRET|PASSWORD|API_KEY|TOKEN|PRIVATE_KEY))=(.*)$", line)
        if m:
            value = m.group(2).split("#")[0].strip().strip('"')
            assert value in {"", "__SECRET__", "__PEM_PRIVATE_KEY__"} or value.startswith("__"), (
                line
            )


def test_production_manifests_set_the_proxy_trust_and_disable_docs() -> None:
    cm = yaml.safe_load((ROOT / "infrastructure/kubernetes/base/configmap.yaml").read_text())
    assert cm["data"]["HARDENING_TRUSTED_PROXY_HOPS"] == "1"
    assert cm["data"]["HARDENING_DOCS_ENABLED"] == "false"


def test_vulnerable_jwt_stack_is_gone_from_the_locked_dependencies() -> None:
    import tomllib

    names = {p["name"] for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    for banned in ("python-jose", "ecdsa"):
        assert banned not in names
    assert "pyjwt" in names
    assert "jose" not in (ROOT / "pyproject.toml").read_text().lower()
