"""Docs must not drift: paths they cite exist, every document is indexed, generated references match the code."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP_PARTS = {"node_modules", "build", ".terraform", "dist", ".venv"}
PATH_RE = re.compile(
    r"`((?:docs|scripts|packages|apps|rag|tests|infrastructure|monitoring|data|migrations|\.github)"
    r"/[A-Za-z0-9_./*\-]+?)(?:::[A-Za-z0-9_\[\]\-]+)?(?:#[\w-]+)?`"
)
# Historical documents that deliberately describe things that were planned and later removed.
HISTORICAL = {
    ("docs/adr/ADR-002-modular-monolith.md", "apps/ingestion-service"),
    ("docs/adr/ADR-002-modular-monolith.md", "apps/worker-service"),
    ("docs/adr/ADR-002-modular-monolith.md", "packages/schemas"),
    ("docs/architecture/PHASE_0_ARCHITECTURE.md", "packages/schemas"),
    ("docs/database/PHASE_3_DATABASE_DESIGN.md", "migrations/versions/9d3816ce05b0_...py"),
}


def _markdown() -> list[Path]:
    return [
        p
        for p in ROOT.rglob("*.md")
        if not (SKIP_PARTS & set(p.relative_to(ROOT).parts)) and "egg-info" not in str(p)
    ]


def test_every_repository_path_cited_in_the_docs_exists() -> None:
    missing = []
    for f in _markdown():
        rel = str(f.relative_to(ROOT))
        for m in PATH_RE.finditer(f.read_text(errors="ignore")):
            p = m.group(1).rstrip(".,:")
            if "*" in p or "<" in p or (rel, p) in HISTORICAL:
                continue
            if not (ROOT / p).exists():
                missing.append(f"{rel}: {p}")
    assert not missing, "\n".join(missing)


def test_every_document_is_listed_in_the_docs_index() -> None:
    index = (ROOT / "docs/README.md").read_text()
    unindexed = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "docs").rglob("*.md")
        if p.name != "README.md"
        and "results" not in p.parts
        and f"`{p.relative_to(ROOT)}`" not in index
    ]
    assert not unindexed, f"add to docs/README.md: {unindexed}"


def test_every_alert_has_a_runbook_section_and_every_runbook_section_has_an_alert() -> None:
    import yaml

    rules = yaml.safe_load((ROOT / "monitoring/prometheus/alerts.yaml").read_text())
    alerts = {r["alert"] for g in rules["groups"] for r in g["rules"]}
    doc = (ROOT / "docs/operations/PHASE_29_OBSERVABILITY.md").read_text()
    runbook = set(re.findall(r"^### (Ekc\w+)$", doc, re.MULTILINE))
    assert alerts == runbook


def test_error_codes_named_in_troubleshooting_exist_in_code() -> None:
    text = (ROOT / "docs/troubleshooting/TROUBLESHOOTING.md").read_text()
    named = set(re.findall(r"`([A-Z][A-Z_]{5,})`", text))
    source = "\n".join(
        p.read_text() for root in ("packages", "apps/api") for p in (ROOT / root).rglob("*.py")
    )
    env_vars = (ROOT / "docs/reference/CONFIGURATION.md").read_text()
    unknown = sorted(
        c for c in named if c not in source and f"`{c}`" not in env_vars and c != "SASL_SSL"
    )
    assert not unknown, f"troubleshooting names codes that do not exist: {unknown}"


def test_metrics_named_in_troubleshooting_exist() -> None:
    text = (ROOT / "docs/troubleshooting/TROUBLESHOOTING.md").read_text()
    registry = (ROOT / "packages/observability/metrics.py").read_text()
    missing = sorted(
        m for m in set(re.findall(r"`(ekc_[a-z_]+)`", text)) if f'"{m}"' not in registry
    )
    assert not missing, missing


@pytest.mark.parametrize("name", ["CONFIGURATION.md", "API.md"])
def test_generated_reference_docs_match_the_code(name: str) -> None:
    from scripts import gen_reference_docs as g

    committed = (ROOT / "docs/reference" / name).read_text()
    assert committed == g.FILES[name](), "run: python scripts/gen_reference_docs.py"


def test_portfolio_material_states_the_limits_next_to_the_claims() -> None:
    summary = (ROOT / "docs/interview/PORTFOLIO_SUMMARY.md").read_text()
    guide = (ROOT / "docs/interview/INTERVIEW_GUIDE.md").read_text()
    assert "no-go" in summary and "never run in production" in summary
    assert "Things not to say" in guide and "production ready" in guide


def test_numbers_quoted_in_interview_material_trace_to_stored_results() -> None:
    """Spot-check the headline figures against the committed result files."""
    import json

    pool = json.loads(
        (ROOT / "docs/performance/results/query_phase45_pool5_overload.json").read_text()
    )
    after = json.loads(
        (
            ROOT / "docs/performance/results/query_phase45_pool5_after_connection_fix.json"
        ).read_text()
    )
    before16 = next(p for p in pool["results"]["query"] if p["concurrency"] == 16)
    after16 = next(p for p in after["results"]["query"] if p["concurrency"] == 16)
    assert before16["throughput_rps"] == 1.6 and before16["errors"] == 18
    assert round(after16["throughput_rps"]) == 31 and after16["errors"] == 0
