"""Regression floors for advisories found by pip-audit (the CI scan is the authority; this keeps a
revert from slipping by). Versions are read from uv.lock, the single source of truth."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# package -> first fixed version, from the Phase 44 audit (2026-10-25)
FIXED_IN = {"pypdf": (6, 21, 2), "certifi": (2026, 10, 19)}


def _ver(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def test_locked_versions_are_at_or_above_the_fixed_releases() -> None:
    pins = {
        p["name"]: p["version"] for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    }
    for pkg, fixed in FIXED_IN.items():
        assert _ver(pins[pkg]) >= fixed, f"{pkg} {pins[pkg]} is below the fixed release"


def test_pyproject_does_not_allow_the_vulnerable_pypdf_range() -> None:
    assert '"pypdf>=6.21.2"' in (ROOT / "pyproject.toml").read_text()


def test_deployed_redis_urls_use_tls() -> None:
    for rel in (
        "infrastructure/kubernetes/base/configmap.yaml",
        "infrastructure/helm/ekc/values.yaml",
    ):
        text = (ROOT / rel).read_text()
        assert "REDIS_URL: rediss://" in text and "REDIS_URL: redis://" not in text, rel
