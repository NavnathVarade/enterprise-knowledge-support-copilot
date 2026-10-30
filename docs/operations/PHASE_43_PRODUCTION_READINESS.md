# Phase 43 — Production Readiness Review

> **Phase 50 note:** configuration is now environment-aware (ADR-011). The verdict is unchanged: no run against a real broker was added, and the new Kafka reachability checks are only mock-tested.

**Verdict: NO-GO for production traffic today.** The repository is configured to a production standard on everything a static review can see, but several things a production launch depends on have never been executed (list below). This document is the gate; close the blockers, re-run the checker, update the table.

## 1. Executable review

`python scripts/readiness_review.py [--json PATH] [--kustomize PATH]` renders both Kustomize overlays and checks the repo. It exits 1 on any FAIL; `tests/unit/test_readiness_review.py` proves each check *fails* on a bad input (so green means something) and passes on this repo. Last output: `docs/operations/readiness_report.json`.

| Area | Check | Result |
|---|---|---|
| Pod security | non-root, read-only rootfs, no privilege escalation, all capabilities dropped (staging, production) | PASS |
| Resources | requests + memory limit on every container | PASS |
| Images | pinned tags/digests, never `:latest` (manifests and Dockerfile bases) | PASS |
| Availability | liveness + readiness on every Deployment; PodDisruptionBudget for every Deployment | PASS (after fixes below) |
| Network | default-deny ingress+egress NetworkPolicy | PASS |
| Secrets | no Secret with data in manifests; `.gitignore` covers `.env`, state, `*.tfvars` | PASS (after fix) |
| Containers | final `USER` non-root in both Dockerfiles | PASS |
| Database | one Alembic head; every revision has a non-empty `downgrade()` | PASS (reversibility also exercised by CI/regression) |
| Observability | all 13 alerts link to a runbook heading that exists | PASS |
| Open risks | markers of "not run / not measured" across docs | 72 reported, not hidden |

### Defects found by this review and fixed
1. Worker Deployments had no readiness probe, so a bad rollout of the ingestion worker could replace healthy pods. Added (same heartbeat check) in Kustomize base and Helm.
2. `ekc-outbox-relay` had no PodDisruptionBudget. Added `maxUnavailable: 1` (single replica by design; a stricter budget would block node drains, and the outbox table buffers events during the gap).
3. `.gitignore` only covered `envs/*/terraform.tfvars`; now `*.tfvars` (the `.example` files stay tracked).

## 2. Blockers before production traffic

| # | Blocker | Why it matters | Closes when |
|---|---|---|---|
| 1 | Container images never built or scanned (Docker Hub unreachable in the build sandbox) | Dockerfiles are linted (hadolint), not proven | CI `container-build` green on GitHub, plus an image vulnerability scan added |
| 2 | Terraform never `validate`d/`plan`ned (provider registry unreachable); Helm chart never rendered | Only `fmt` and structural tests ran | `tofu validate/plan` against a real account; the existing CI steps (`helm lint/template`, `tofu validate`) run green on GitHub, then a real `plan` |
| 3 | Never run against real Kafka/MSK, S3, an LLM or embedding provider | Delivery semantics, throughput, provider errors are unobserved | Staging soak with real dependencies |
| 4 | RPO/RTO **NOT MEASURED** (only a logical-restore drill on a seeded DB); no region-loss plan | Recovery promises would be guesses | Timed restore of a production-sized snapshot; documented RPO/RTO |
| 5 | Performance **NOT MEASURED** on production-like infrastructure | Sandbox numbers do not transfer | Load test in staging against agreed SLOs |
| 6 | Auth gaps: no refresh tokens, token revocation, MFA, JWT key rotation | A leaked token is valid until expiry | Identity-provider integration or implemented revocation + rotation |
| 7 | ~~TLS to Postgres and Redis not enforced by application code~~ **Closed in Phase 44** (settings refuse plaintext DSNs in staging/production); server-side TLS itself still unverified | — | Verify against the real RDS/ElastiCache |
| 8 | No document deletion/retention API | Cannot honour erasure requests | Delete + purge (chunks, embeddings, objects, audit note) implemented and tested |
| 9 | Parsers run in the API/worker process | A malicious file exercises parser bugs in a privileged process | Sandboxed parsing (separate low-privilege job) |
| 10 | No external penetration test; no accessibility audit; E2E is single-browser | Unknown unknowns | Independent test; a11y audit |
| 11 | On-call rotation, ownership (CODEOWNERS entries), incident process are organisational, not in code | Alerts without an owner page nobody | Named owners and an on-call schedule |

## 3. How to use this
The checker is a step of the CI `deploy-config` job (never yet run on GitHub; it needs only `kustomize` and `pyyaml`); treat any FAIL as blocking. Re-evaluate the blocker table before each environment promotion and record the date and evidence next to each closed item. Do not mark an item closed on the strength of a document alone.
