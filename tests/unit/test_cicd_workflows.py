"""Policy tests over the GitHub Actions workflows and the smoke-test script.

The workflows themselves have never run on GitHub (no access from the build environment);
`actionlint` (when installed) and these static policies are what protect them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from scripts import smoke_test

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows"
FILES = sorted(WF.glob("*.yml"))


def _load(name: str) -> dict[str, Any]:
    d = yaml.safe_load((WF / name).read_text())
    d["on"] = d.pop(True, d.get("on"))  # PyYAML parses the bare key `on` as boolean True
    return d  # type: ignore[no-any-return]


def _steps(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for j in doc["jobs"].values() for s in j.get("steps", [])]


def test_actionlint_is_clean_when_available() -> None:
    binary = os.environ.get("ACTIONLINT_BIN") or shutil.which("actionlint")
    if binary is None:
        pytest.skip("actionlint not installed")
    out = subprocess.run(  # noqa: S603  # nosec B603
        [binary, *map(str, FILES)], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_default_permissions_are_read_only(path: Path) -> None:
    doc = _load(path.name)
    assert doc["permissions"] == {"contents": "read"}, "set least-privilege defaults at top level"
    assert "write-all" not in path.read_text()


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_actions_are_version_pinned_never_floating(path: Path) -> None:
    for uses in re.findall(r"uses:\s*(\S+)", path.read_text()):
        if uses.startswith("./"):
            continue
        assert "@" in uses, uses
        ref = uses.split("@", 1)[1]
        assert ref not in {"main", "master", "latest", "HEAD"}, uses


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_untrusted_event_data_is_never_interpolated_into_scripts(path: Path) -> None:
    risky = re.compile(
        r"\$\{\{\s*(?:github\.event\.(?:inputs|issue|pull_request|comment|head_commit|workflow_run\.head_(?:branch|commit))"
        r"|github\.head_ref|inputs\.)"
    )
    for step in _steps(_load(path.name)):
        run = step.get("run")
        if run:
            assert not risky.search(run), f"script injection risk in step {step.get('name')!r}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_static_cloud_credentials(path: Path) -> None:
    text = path.read_text()
    assert not re.search(r"AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)", text)
    if (
        path.name != "ci.yml"
    ):  # ci.yml's throwaway Postgres *service container* uses a fixed test password
        assert not re.search(r"(?i)password:\s*[\"']?[A-Za-z0-9]{8,}", text.replace("secrets.", ""))


def test_ci_has_concurrency_and_a_timeout_on_every_job() -> None:
    ci = _load("ci.yml")
    assert "concurrency" in ci
    for name, job in ci["jobs"].items():
        assert "timeout-minutes" in job, name


def test_ci_signs_images_and_only_pushes_from_main() -> None:
    text = (WF / "ci.yml").read_text()
    assert "cosign sign --yes" in text and "steps.build.outputs.digest" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert not _load("ci.yml")["on"].get("pull_request_target")


def test_deploy_runs_only_after_ci_on_main_or_manually() -> None:
    on = _load("deploy.yml")["on"]
    assert set(on) == {"workflow_run", "workflow_dispatch"}
    assert on["workflow_run"]["workflows"] == ["CI"] and on["workflow_run"]["branches"] == ["main"]
    jobs = _load("deploy.yml")["jobs"]
    assert "completed" in str(on["workflow_run"]["types"])
    assert "conclusion == 'success'" in jobs["resolve"]["if"]
    # production follows staging in the automatic pipeline
    assert "deploy-staging" in jobs["deploy-production"]["needs"]
    # manual runs must name an immutable digest
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["image_digest"]["required"] is True


def test_deploy_never_builds_and_serialises_per_environment() -> None:
    deploy = _load("deploy.yml")
    assert deploy["concurrency"]["cancel-in-progress"] is False
    text = (WF / "deploy.yml").read_text() + (WF / "_deploy-env.yml").read_text()
    assert "docker/build-push-action" not in text
    assert not re.search(r"docker build(?!x imagetools)", text)


def test_environment_job_is_gated_verified_and_ordered() -> None:
    env_wf = _load("_deploy-env.yml")
    job = env_wf["jobs"]["deploy"]
    assert job["environment"] == "${{ inputs.environment }}"  # approvals attach to the Environment
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    names = [s.get("name", "") for s in job["steps"]]

    def idx(prefix: str) -> int:
        return next(i for i, n in enumerate(names) if n.startswith(prefix))

    # verify signature before touching the cloud, migrate before workloads, smoke after rollout
    assert idx("Verify both images") < idx("Configure AWS credentials")
    assert idx("Stage 1") < idx("Stage 2") < idx("Stage 3") < idx("Smoke test")
    assert idx("Smoke test") < idx("Roll back on failure")
    rollback = next(s for s in job["steps"] if s.get("name") == "Roll back on failure")
    assert rollback["if"] == "failure()"
    assert "rollout-started" in rollback["run"], "rollback must not undo unrelated releases"
    assert "--certificate-identity" in (WF / "_deploy-env.yml").read_text()


def test_deploy_uses_oidc_role_from_environment_variables() -> None:
    text = (WF / "_deploy-env.yml").read_text()
    assert "role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}" in text
    assert "secrets.AWS" not in text


def test_dependabot_covers_every_ecosystem_in_use() -> None:
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert {u["package-ecosystem"] for u in cfg["updates"]} >= {
        "github-actions",
        "pip",
        "docker",
        "terraform",
    }


def test_codeowners_covers_workflows_and_infrastructure() -> None:
    text = (ROOT / ".github" / "CODEOWNERS").read_text()
    assert "/.github/" in text and "/infrastructure/" in text and "REPLACE" in text


# ---- smoke test script ---------------------------------------------------------------------


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(handler))


def test_smoke_passes_on_a_healthy_hardened_deployment() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        h = {
            "x-content-type-options": "nosniff",
            "x-request-id": "r",
            "strict-transport-security": "max-age=1",
        }
        if req.url.path in {"/metrics", "/docs", "/openapi.json", "/redoc"}:
            return httpx.Response(404, headers=h)
        if req.url.path == "/api/v1/auth/login":
            return httpx.Response(401, headers=h)
        return httpx.Response(200, json={"status": "ok"}, headers=h)

    with _client(handler) as c:
        results = [chk(c, True) for chk in smoke_test.CHECKS if chk is not smoke_test.check_ready]
        results.append(smoke_test.check_ready(c, True, attempts=1, delay=0))
    assert all(r.ok for r in results), [r for r in results if not r.ok]


def test_smoke_fails_when_metrics_docs_or_hsts_are_exposed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        h = {"x-content-type-options": "nosniff", "x-request-id": "r"}
        if req.url.path == "/openapi.json":
            return httpx.Response(200, json={"openapi": "3.1.0"}, headers=h)
        if req.url.path == "/docs":
            return httpx.Response(
                200, text="<title>Swagger UI</title> swagger-ui-bundle", headers=h
            )
        return httpx.Response(200, headers=h)

    with _client(handler) as c:
        assert not smoke_test.check_metrics_not_public(c, True).ok
        docs = smoke_test.check_docs_hidden(c, True)
        assert not docs.ok and "/openapi.json" in docs.detail and "/docs" in docs.detail
        assert not smoke_test.check_headers(c, True).ok  # production requires HSTS
        assert smoke_test.check_headers(c, False).ok


def test_smoke_is_not_fooled_by_the_spa_shell_answering_unknown_paths() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<!doctype html><title>Knowledge Copilot</title>")

    with _client(handler) as c:
        assert smoke_test.check_docs_hidden(c, True).ok


def test_smoke_treats_a_5xx_login_as_failure_and_ready_retries() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/login"):
            return httpx.Response(500)
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200)

    with _client(handler) as c:
        assert not smoke_test.check_bad_login(c, False).ok
        assert smoke_test.check_ready(c, False, attempts=5, delay=0).ok
    assert calls["n"] == 3


def test_smoke_main_refuses_plain_http_for_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["smoke", "--base-url", "http://x.test", "--production"])
    assert smoke_test.main() == 2


def test_web_image_is_built_signed_scanned_verified_and_deployed_with_the_api_image() -> None:
    ci = (WF / "ci.yml").read_text()
    assert "apps/web/Dockerfile" in ci and "steps.build_web.outputs.digest" in ci
    assert "ekc-web:${{ github.sha }}" in ci  # scanned by Trivy
    assert "npm audit --omit=dev --audit-level=high" in ci
    env = (WF / "_deploy-env.yml").read_text()
    assert '"$WEB_IMAGE_REPO@$WEB_DIGEST"' in env  # signature verified before cloud access
    assert '"ekc-web=$WEB_IMAGE_REPO@$WEB_DIGEST"' in env
    assert (
        env.count("ekc-api ekc-web ekc-ingestion-worker ekc-outbox-relay") >= 3
    )  # record, roll out, roll back
    assert "web_image_digest" in (WF / "deploy.yml").read_text()


def test_dependabot_covers_the_frontend() -> None:
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    dirs = {(u["package-ecosystem"], u["directory"]) for u in cfg["updates"]}
    assert ("npm", "/apps/web") in dirs and ("docker", "/apps/web") in dirs


def test_ci_has_a_browser_e2e_job_that_gates_container_build() -> None:
    jobs = _load("ci.yml")["jobs"]
    assert "e2e" in jobs
    steps = " ".join(str(s.get("run", "")) for s in jobs["e2e"]["steps"])
    assert "playwright install" in steps and "pytest tests/e2e" in steps
    assert "e2e" in jobs["container-build"]["needs"]
