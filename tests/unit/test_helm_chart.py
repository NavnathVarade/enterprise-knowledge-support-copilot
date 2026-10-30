"""Static checks for the Helm chart.

IMPORTANT: the `helm` binary is not available in the build environment, so these tests do NOT
render the templates. They are a structural lint (values/schema consistency, template block
balance, helper definitions, drift against the Kustomize base). Rendering with `helm lint` and
`helm template` is a CI step (see .github/workflows/ci.yml) that has not been executed here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infrastructure" / "helm" / "ekc"
TEMPLATES = sorted((CHART / "templates").iterdir())


def _values() -> dict[str, Any]:
    return yaml.safe_load((CHART / "values.yaml").read_text())  # type: ignore[no-any-return]


def _all_text() -> str:
    return "\n".join(p.read_text() for p in TEMPLATES)


def test_chart_metadata() -> None:
    meta = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert meta["apiVersion"] == "v2" and meta["name"] == "ekc" and meta["type"] == "application"
    assert re.fullmatch(r"\d+\.\d+\.\d+", meta["version"])


def test_values_validate_against_the_schema_once_required_values_are_set() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((CHART / "values.schema.json").read_text())
    vals = _values()
    vals["image"].update(repository="ghcr.io/example/ekc", tag="abc123")
    vals["web"]["image"].update(repository="ghcr.io/example/ekc-web", tag="abc123")
    jsonschema.validate(vals, schema)
    for overlay in ("values-staging.yaml", "values-production.yaml"):
        merged = {**vals, **yaml.safe_load((CHART / overlay).read_text())}
        jsonschema.validate(merged, schema)
    vals["image"]["tag"] = "latest"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(vals, schema)


def test_defaults_contain_no_secrets_registry_or_real_host() -> None:
    vals = _values()
    assert vals["image"]["repository"] == "" and vals["image"]["tag"] == ""
    assert vals["web"]["image"]["repository"] == "" and vals["web"]["image"]["tag"] == ""
    assert vals["ingress"]["host"] == ""
    flat = json.dumps(vals)
    assert not re.search(r"localhost|127\.0\.0\.1", flat)
    for key in vals["config"]:
        assert not re.search(r"(?i)password|secret|api_key|private_key|token", key), key


def test_every_values_reference_resolves() -> None:
    vals = _values()
    missing = []
    for ref in sorted(set(re.findall(r"\.Values\.([A-Za-z0-9_.]+)", _all_text()))):
        node: Any = vals
        for part in ref.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(ref)
                break
            node = node[part]
    assert not missing, missing


def test_overlay_values_only_override_known_keys() -> None:
    def keys(d: dict[str, Any], prefix: str = "") -> set[str]:
        out: set[str] = set()
        for k, v in d.items():
            out.add(prefix + k)
            if isinstance(v, dict):
                out |= keys(v, f"{prefix}{k}.")
        return out

    known = keys(_values())
    for name in ("values-staging.yaml", "values-production.yaml"):
        assert keys(yaml.safe_load((CHART / name).read_text())) <= known, name


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_blocks_are_balanced(path: Path) -> None:
    text = path.read_text()
    # YAML flow maps legitimately end in "}}" outside any action, so compare real actions only:
    # after removing every complete `{{ ... }}`, no stray opener may remain.
    assert "{{" not in re.sub(r"\{\{.*?\}\}", "", text, flags=re.S)
    opens = len(re.findall(r"\{\{-?\s*(?:if|range|with|define)\b", text))
    ends = len(re.findall(r"\{\{-?\s*end\b", text))
    assert opens == ends, f"{path.name}: {opens} blocks opened, {ends} closed"


def test_every_included_helper_is_defined() -> None:
    text = _all_text()
    defined = set(re.findall(r'define "([^"]+)"', text))
    used = set(re.findall(r'include "([^"]+)"', text))
    assert used and used <= defined, used - defined


def test_security_posture_is_templated_for_every_workload() -> None:
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    for needle in (
        "runAsNonRoot: true",
        "automountServiceAccountToken: false",
        "readOnlyRootFilesystem: true",
        "drop: [ALL]",
        "RuntimeDefault",
    ):
        assert needle in helpers, needle
    for name in ("api.yaml", "web.yaml", "workers.yaml", "jobs.yaml"):
        body = (CHART / "templates" / name).read_text()
        assert 'include "ekc.podSecurity"' in body and 'include "ekc.containerSecurity"' in body


def test_no_rbac_and_no_secret_objects_in_the_chart() -> None:
    kinds = set(re.findall(r"^kind: (\w+)", _all_text(), flags=re.M))
    assert not kinds & {"Secret", "Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}


def test_hooks_cover_what_the_migration_job_needs() -> None:
    """Pre-install hooks run before regular resources: config and identity must be hooks too."""
    for name in ("configmap.yaml", "serviceaccount.yaml", "jobs.yaml"):
        assert '"helm.sh/hook": pre-install,pre-upgrade' in (CHART / "templates" / name).read_text()


def test_chart_config_matches_the_kustomize_configmap() -> None:
    """Both deployment paths must ship the same non-secret settings (drift guard)."""
    base = yaml.safe_load((ROOT / "infrastructure/kubernetes/base/configmap.yaml").read_text())
    kustomize_keys = set(base["data"]) - {"ENVIRONMENT"}
    assert set(_values()["config"]) == kustomize_keys
    for k in kustomize_keys:
        assert str(_values()["config"][k]) == str(base["data"][k]), k


def test_chart_resources_match_the_kustomize_base() -> None:
    api = yaml.safe_load_all((ROOT / "infrastructure/kubernetes/base/api.yaml").read_text())
    dep = next(d for d in api if d["kind"] == "Deployment")
    assert (
        dep["spec"]["template"]["spec"]["containers"][0]["resources"]
        == _values()["api"]["resources"]
    )


def test_chart_web_resources_match_the_kustomize_base() -> None:
    docs = yaml.safe_load_all((ROOT / "infrastructure/kubernetes/base/web.yaml").read_text())
    dep = next(d for d in docs if d["kind"] == "Deployment")
    assert (
        dep["spec"]["template"]["spec"]["containers"][0]["resources"]
        == _values()["web"]["resources"]
    )
    assert dep["spec"]["replicas"] == _values()["web"]["replicas"]
    assert 'include "ekc.webImage"' in (CHART / "templates" / "web.yaml").read_text()


def test_chart_ingress_routes_api_and_web_like_the_kustomize_base() -> None:
    text = (CHART / "templates" / "ingress.yaml").read_text()
    assert "path: /api" in text and "-api, port" in text and "-web, port" in text
