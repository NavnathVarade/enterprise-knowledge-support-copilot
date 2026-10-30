# CI/CD (Phase 39)

Three workflows in `.github/workflows/`:

| File | Trigger | Does |
|---|---|---|
| `ci.yml` | push to main, every PR | ruff, mypy strict, tests on pgvector + Redis (nginx gateway included), migration up/down/up, bandit, pip-audit (on `uv export` of the lock), gitleaks, hadolint, kustomize + kubeconform, Helm, tofu fmt/validate, compose, promtool; then build image (push on main only) with SBOM + provenance, **cosign keyless signature**, Trivy |
| `deploy.yml` | CI succeeded on main, or manual (`image_digest` + target) | resolves the commit's image tag to an immutable digest; staging, then production |
| `_deploy-env.yml` | called by deploy.yml | one environment: verify -> authenticate -> render -> staged apply -> rollout -> smoke test -> auto-rollback |

## STATUS: reviewed, NEVER run on GitHub

I have no GitHub or cloud access here. What was verified: **`actionlint` 1.7.3 passes on all three
workflows**, 25 policy tests (`tests/unit/test_cicd_workflows.py`), and the smoke-test script runs
for real against nginx + uvicorn (`tests/integration/test_gateway_nginx.py`). Everything that
needs GitHub, GHCR, AWS OIDC, a cluster or cosign's transparency log is **NOT RUN**.

## Security design

- Top-level `permissions: contents: read` everywhere; `id-token: write` only on the deploy job and
  `packages: write` only on the image-build job. Least privilege is tested.
- Third-party actions are pinned to a version tag (never `main`). Stronger: pin by commit SHA;
  Dependabot (weekly) proposes updates.
- **Build once, deploy the digest.** Deploy never builds. Staging and production receive the same
  `@sha256:` image; tags are never deployed.
- **Signature verification before cloud access:** `cosign verify` requires the signing identity to
  be this repository's `ci.yml` on `refs/heads/main`. An unsigned or foreign image never reaches AWS.
- **No stored cloud credentials.** AWS access is GitHub OIDC -> a role per environment
  (`AWS_DEPLOY_ROLE_ARN`), short-lived. Per-environment settings are Environment *variables*.
- **No script injection:** untrusted event data is only passed through `env:`, never interpolated
  into `run:` (tested).
- Deploys are serialised per environment and never cancelled mid-rollout.

## Rollout order (and why)

1. **Stage 1** namespace/config/policies/Service/HPA/PDB (everything without a workload label).
2. **Stage 2** delete the previous `ekc-migrate`/`ekc-topics-init` Jobs (immutable templates), apply
   them, wait up to 15 min. Plain `kubectl apply` ignores Argo CD `PreSync` hooks, so the workflow
   enforces migrate-before-rollout itself. A failed migration stops the pipeline before any new code runs.
3. **Stage 3** Deployments, then `rollout status` for API, worker and relay (10 min each).
4. **Smoke test** against the public URL (`scripts/smoke_test.py`): liveness, readiness (retried),
   security headers (+HSTS in production), `/metrics` not public, bad login is 401 not 5xx, docs hidden in production.
5. **Rollback** on any failure *after Stage 3 started*: `kubectl rollout undo` per Deployment. A
   failure in stage 1-2 deliberately rolls nothing back (it would revert an unrelated release).

## Limits you must know

- **Database migrations are not rolled back.** Write them expand/contract (add -> backfill -> switch ->
  drop in a later release) so the previous version still runs against the new schema.
- A ConfigMap-only change does not restart pods (no config hash). A deploy always changes the image
  digest, so this only matters for out-of-band edits: `kubectl rollout restart`.
- The smoke test proves reachability and basic hardening, not functional correctness or performance.
- No canary/blue-green, no automatic metrics-based promotion; production is a manual approval.
- Digest discovery relies on CI tagging the image with the commit SHA (it does).
- No contract tests or DAST yet.

## One-time setup (cannot be done from code)

1. GitHub Environments `staging` and `production`; **required reviewers on `production`**, branch
   restriction to `main`.
2. Environment variables on each: `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `EKS_CLUSTER_NAME`, `EKC_HOST`.
3. AWS: IAM OIDC provider for `token.actions.githubusercontent.com`; a deploy role per environment whose
   trust policy is limited to `repo:<owner>/<repo>:environment:<env>` and that is mapped to a
   Kubernetes RBAC role (namespace `ekc` only) through EKS access entries.
4. Branch protection on `main`: required checks (all CI jobs), required reviews, CODEOWNERS
   (replace the `REPLACE_ME_ORG` teams in `.github/CODEOWNERS`).
5. Settings: allow GitHub Actions to create PRs (Dependabot), package visibility for GHCR.
