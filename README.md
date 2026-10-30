# Enterprise Knowledge & Support Copilot

A permission-aware, multi-tenant **RAG platform** for enterprise knowledge — policies,
engineering docs, support tickets, incidents, contracts. Authorization is enforced **inside the
retrieval query**, before any content can reach the LLM; every answer is cited, every action is
audited in a tamper-evident chain, and every cost is metered per tenant.

> **Run it:** [`HOW_TO_RUN.md`](HOW_TO_RUN.md) — development on your own machine (no Docker, no Kafka)
> and production, every command in order. **Index of all docs:** [`docs/README.md`](docs/README.md) ·
> [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`SECURITY.md`](SECURITY.md)

## Why it is built this way

| Principle | What it means in the code |
|---|---|
| **Authorization in the query** | Tenant and ACL predicates are part of the SQL that fetches chunks (vector, lexical, hybrid, reranked), then re-checked when the context is built (ADR-003). A chunk the user may not see is never fetched. |
| **Honest by construction** | Unmeasured numbers are labelled `NOT YET MEASURED`, unrun code `NOT RUN`. Unknown cost is `NULL`, never `0`. No model key means no answer — never a fake one. |
| **Durable events** | Uploads write a transactional **outbox** row; Kafka (production) or the in-process dispatcher (development) delivers it at-least-once to an idempotent pipeline (ADR-004, ADR-011). |
| **Two explicit modes** | `development` runs on a laptop; `production` refuses to start without Kafka, TLS and explicit endpoints, and refuses every development substitute (ADR-011). |
| **Boundaries enforced** | Layering and no-import-cycles are checked by a test and in CI (ADR-009). |

## Architecture at a glance

```
 browser ─► web (nginx, React SPA) ─► api (FastAPI) ──► PostgreSQL + pgvector  (identity, documents, chunks,
                                        │    │                                  embeddings, audit, usage, OUTBOX)
                                        │    └──────► Redis   (login throttle, rate limits, breakers, idempotency)
                                        │
        POST /documents ─► object store (S3 | local disk in dev) + outbox row  ── same transaction
                                        │
   production:  outbox-relay ─► Kafka ─► ingestion-worker ─┐          development:  in-process dispatcher
                                                           ▼                         (reads the same outbox)
                         parse ─► chunk ─► embed ─► index (atomic, idempotent) ─► document "active"

 POST /query ─► route ─► hybrid retrieval (vector + full-text, RRF) ─► ACL + temporal filter ─► rerank
             ─► context build (budget, dedup, citations) ─► LLM gateway (retry, fallback, budget) ─► validated cited answer
```

Routes: `/api/v1/auth/login`, `/auth/signup` (new organization; off by default in production), `/documents`, `/query`, `/usage/*`, `/audit/*`, `/health/live|ready`
(`docs/reference/API.md`). Design per area: `docs/README.md`.

![Ask with a cited answer](docs/screenshots/08-ask-answer-with-citation.png)

More pictures of every page (light, dark, phone): `docs/screenshots/README.md`.

## Quick start (development, no Docker, no Kafka)

```powershell
uv sync --frozen
uv run python -m scripts.init_dev_env --database-password "your-local-password"   # .env + JWT keys
uv run python -m scripts.dev_database --admin-password "<postgres superuser pw>"  # role + db + pgvector
uv run alembic upgrade head
uv run python -m scripts.seed_database                                            # demo tenants/users
uv run uvicorn apps.api.app.main:app --reload --reload-dir apps
```

Needs PostgreSQL 16 with **pgvector** and Redis running locally. Log in as
`alice@example.com` / `alice-synthetic-pw-1` (tenant `acme-corp`), upload a document, watch it become
`active`. Add `LLM_API_KEY` (and `EMBEDDING_API_KEY`) to `.env` for generated answers and real
semantic retrieval. Full walkthrough, Windows notes, troubleshooting and the production procedure:
[`HOW_TO_RUN.md`](HOW_TO_RUN.md).

## Development vs production

| | `ENVIRONMENT=development` | `ENVIRONMENT=production` / `staging` |
|---|---|---|
| Kafka | off (`KAFKA_ENABLED=false`), never contacted | **required**; `KAFKA_ENABLED=false` is rejected |
| Event handling | in-process dispatcher over the Postgres outbox | relay → Kafka → consumer group (+ DLQ) |
| Objects | local disk or S3 | S3 bucket required |
| Redis / Postgres | localhost defaults / local URL | explicit, TLS, no localhost |
| Config source | `.env` (template: `.env.example`) | real environment / secret manager (template: `.env.production.example`) |
| If `ENVIRONMENT` is unset | — | **production rules apply** |

Settings errors name the setting and rule but never print values. Precedence: environment variables >
`.env` > defaults. Details and the exact semantics (and limits) of the dev dispatcher:
`docs/adr/ADR-011-environment-aware-configuration.md`.

## Repository layout

```
apps/api            FastAPI service (routes, middleware, health)      apps/web      React + TypeScript SPA
packages/           config, domain, database, auth, security, kafka, storage, ingestion, llm, usage,
                    orchestration, analytics, investigation, workers, observability, providers
rag/                parsing, chunking, embeddings, retrieval, reranking, context, generation, citations, evaluation
migrations/         Alembic (7 revisions)     scripts/   seed, dev setup, topics, benchmarks, readiness review
infrastructure/     docker, kubernetes (kustomize), helm, terraform (AWS)      docs/   design, ADRs, runbooks
tests/              unit · integration · resilience · retrieval · security · contract · e2e (Playwright)
```

## Quality gates

`make check` runs what CI runs: `uv lock --check`, ruff, strict mypy, the full test suite, plus
`scripts/architecture_check.py`, `scripts/readiness_review.py`, bandit, pip-audit, gitleaks. Last full
run: **1,098 backend tests passed, 0 failed** (2 skipped: the screenshot-capture tests, which need `EKC_SCREENSHOT_DIR`; includes 20 real-browser tests, run with kustomize/tofu/actionlint on `PATH`) and 88
frontend tests. Commands: `HOW_TO_RUN.md` Part C.

## Status (verified vs not)

**Nothing below is claimed without a passing test or an explicit "not yet" label.** The project was
built phase by phase (`docs/architecture/PHASE_0_ARCHITECTURE.md`); the Phase 43 readiness review's
verdict is **NO-GO for production today** — see `docs/operations/PHASE_43_PRODUCTION_READINESS.md`.
Never run: Docker image builds on the real base, Helm render, Terraform plan/apply, GitHub workflows,
anything against a real Kafka broker, S3, LLM provider or cluster; recovery objectives and real-hardware
performance are not measured.

| Area | Status |
|---|---|
| Typed config + no-localhost-in-prod invariant | ✅ implemented, tested |
| Structured logging + secret redaction | ✅ implemented |
| Domain error taxonomy + safe API error contract | ✅ implemented, tested |
| API service skeleton (`/health/live`, `/health/ready`) | ✅ implemented, tested against real Postgres+Redis |
| Container image | ✅ one multi-stage image, many roles (api / ingestion / outbox-relay / migrate / topics-init), non-root uid 10001, pinned deps. hadolint-clean; **built and run in the sandbox on a substitute Ubuntu 24.04 base** (Docker Hub unreachable) — behaviour on `python:3.12-slim-bookworm` not verified, no registry push — see `docs/deployment/PHASE_30_DEPLOYMENT_DESIGN.md` |
| Docker Compose reference environment | ✅ api + workers + migrate + topics-init + minio; `docker compose config` valid — **full stack not run** (broker/DB images not pullable here) |
| CI workflow | ✅ lint/type/test, hadolint, kustomize+kubeconform, compose check, build with SBOM+provenance, Trivy — each local step verified; **workflow not run on GitHub** |
| Database schema / migrations | ✅ identity + document domains, tested — see `docs/database/PHASE_3_DATABASE_DESIGN.md` |
| Auth / RBAC / tenancy | ✅ JWT auth, login, tenant isolation proven over real HTTP — see `docs/security/PHASE_4_AUTHORIZATION_DESIGN.md` |
| Embeddings + vector storage | ✅ provider abstraction, real Voyage client (mock-tested), deterministic local provider, pgvector storage/query proven — **no live embedding provider reachable here; semantic quality NOT YET MEASURED** — see `docs/rag/PHASE_11_EMBEDDING_DESIGN.md` |
| Lexical search | ✅ Postgres full-text search, tenant-scoped, GIN-index-proven — see `docs/retrieval/PHASE_13_LEXICAL_SEARCH_DESIGN.md` — **not BM25, honestly labeled** |
| Hybrid retrieval (RRF) | ✅ real vector + lexical fusion, dedup proven, tenant-scoped — see `docs/retrieval/PHASE_14_HYBRID_RETRIEVAL_DESIGN.md` |
| Reranking | ✅ real lexical-overlap reranker + Voyage rerank client (mock-tested) — see `docs/retrieval/PHASE_15_RERANKING_DESIGN.md` — **not a neural cross-encoder** |
| ACL-aware retrieval | ✅ authorization enforced in the SQL query itself, proven across vector/lexical/hybrid/reranked retrieval + GET/POST /documents — see `docs/retrieval/PHASE_16_ACL_AWARE_RETRIEVAL_DESIGN.md` |
| Temporal retrieval | ✅ current-vs-historical resolution enforced in the retrieval query, version comparison ACL-enforced — see `docs/retrieval/PHASE_17_TEMPORAL_RETRIEVAL_DESIGN.md` |
| Context construction | ✅ final ACL + temporal re-check, dedup, per-document cap, version consistency, token budget, citation metadata — see `docs/rag/PHASE_18_CONTEXT_CONSTRUCTION_DESIGN.md` |
| LLM gateway | ✅ provider abstraction, bounded retry/backoff/jitter, fallback, per-tenant rate limit, cost accounting (Anthropic client mock-tested, NOT network-verified) — see `docs/llm/PHASE_19_LLM_GATEWAY_DESIGN.md` |
| Answer generation & citations | ✅ versioned prompts + registry, escaped source blocks, citation validation, abstention (model behavior NOT tested against a real LLM) — see `docs/rag/PHASE_20_ANSWER_GENERATION_AND_CITATIONS_DESIGN.md` |
| Query router | ✅ rule-based reference router: 11 routes, time/entity extraction, logged decisions, deterministic fallback (accuracy NOT YET MEASURED; several routes have no executor yet) — see `docs/rag/PHASE_21_QUERY_ROUTER_DESIGN.md` |
| SQL analytics | ✅ semantic layer, strict intents, validated builder, templates, read-only executor, RLS + dedicated read-only role (verified on Postgres; no router wiring or data ingestion yet) — see `docs/analytics/PHASE_22_SQL_ANALYTICS_DESIGN.md` |
| Support & incident investigation | ✅ evidence gathering, temporal-correlation leads (labeled as correlation, not causation), role-gated, tenant-isolated, ACL-enforced knowledge refs (no endpoint or router wiring yet) — see `docs/investigation/PHASE_23_SUPPORT_AND_INCIDENT_INVESTIGATION_DESIGN.md` |
| Prompt-injection defenses | ✅ chunk quarantine, canary, prompt-leak / exfiltration output policy (heuristic detector with documented misses; real-model susceptibility NOT YET MEASURED) — see `docs/security/PHASE_24_PROMPT_INJECTION_DESIGN.md` |
| Query orchestration & API | ✅ `POST /api/v1/query`: routing → retrieval → context → generation → validation, provider wiring with production fail-fast, traces (scripted-LLM tests only; no real provider run; document comparison: see next row) — see `docs/orchestration/PHASE_25_QUERY_ORCHESTRATION_DESIGN.md` |
| Document version comparison | ✅ Authorized two-version fetch (drafts excluded), deterministic chunk diff, no model call when identical, versioned compare prompt, quarantine + budget; chunk-level granularity; real-LLM quality **NOT YET MEASURED** — see `docs/rag/PHASE_26_DOCUMENT_COMPARISON_DESIGN.md` |
| Evaluation framework | ✅ Metrics, strict datasets, routing/retrieval/answer evaluators, leak-detecting harness, CI thresholds (`make eval-*`). Measured: routing 0.81 on a 52-case **dev** set; retrieval/answer numbers use deterministic providers and an extractive baseline (not an LLM) — real-model quality **NOT YET MEASURED** — see `docs/evaluation/PHASE_27_EVALUATION_FRAMEWORK_DESIGN.md` |
| Ingestion pipeline | ✅ Object storage (S3 + moto-tested), transactional outbox, relay, Kafka consumer runner, ingestion worker (parse → chunk → embed → index, atomic, idempotent), poison-message DLQ. Real broker/S3 **NOT VERIFIED**; throughput **NOT YET MEASURED** — see `docs/ingestion/PHASE_28_INGESTION_PIPELINE_DESIGN.md` |
| Observability & audit | ✅ Prometheus metrics (bounded labels, fail-closed `/metrics`), OpenTelemetry tracing with trace continuity across Kafka, hash-chained append-only audit log + API, 13 alerts + dashboard validated against real metric names. OTLP/Prometheus/Grafana **NOT VERIFIED live**; thresholds **untuned** — see `docs/operations/PHASE_29_OBSERVABILITY.md` |
| Cost monitoring & budgets | ✅ Usage ledger (every LLM call and ingestion embedding batch), per-tenant daily token/cost budgets enforced before each LLM call (429 `BUDGET_EXCEEDED`), per-model prices (fallback never borrows the primary's), usage/cost-per-query/cost-per-document API. Unknown cost is NULL, never 0. Query-time embeddings and rerank calls **not metered**; prices are operator-supplied, not fetched — see `docs/operations/PHASE_31_COST_MONITORING.md` |
| Security hardening | ✅ Login throttle (per account/IP, no enumeration oracle), security headers, request-size limits, docs gated off in production, PyJWT with asymmetric-only allow-list (python-jose removed), upload content sniffing and zip-bomb/page limits, trusted-proxy handling, seed and test-key guards, blocking bandit/pip-audit/gitleaks in CI. No MFA/refresh/revocation, no parser sandbox, no pentest — see `docs/security/PHASE_32_SECURITY_HARDENING.md` and `THREAT_MODEL.md` |
| Performance & load testing | ✅ Reproducible benchmarks (retrieval at 10K/100K chunks, HTTP load, chunking) with hardware and dataset recorded; **found and fixed**: generic-plan slowdown (20–70×), bcrypt blocking the event loop, pool exhaustion returning 500; added `RETRIEVAL_HNSW_*` settings and pinned pgvector 0.8. Synthetic data on a shared 2-core sandbox; **1M scale, Kafka, LLM/query end-to-end NOT YET MEASURED**; pgvector < 0.8 returns too few vector results for small tenants — see `docs/performance/PHASE_33_PERFORMANCE.md` |
| Resilience & chaos testing | ✅ Real fault injection (Postgres/Redis stop, table lock, hung dependencies, API SIGTERM/SIGKILL) with before/after results; **fixed**: 500s on Postgres outage, unbounded connect/statement waits, readiness failing on Redis outage, 4 s login penalty with a hung Redis (circuit breaker). Kafka/worker/cluster faults **not run** (no broker/cluster) — see `docs/resilience/PHASE_34_RESILIENCE.md` |
| Docker production reference environment | ✅ Compose topology with nginx gateway, internal data network, resource/log limits, graceful stop, MinIO bucket + analytics-role init jobs, Prometheus/Grafana add-on; gateway behaviour, Prometheus scraping, analytics SQL and graceful shutdown **run for real**; **full stack never brought up (images unreachable), third-party tags unverified**, no TLS — see `docs/deployment/DOCKER.md` |
| Kubernetes hardening | ✅ Worker startup probes, host anti-affinity, ephemeral-storage bounds, worker PDB, HPA-owned replicas, LimitRange/ResourceQuota; explicit no-RBAC decision; kustomize + kubeconform + kube-score **run**; **never applied to a real cluster** — see `docs/deployment/KUBERNETES.md` |
| Helm chart | ⚠️ Chart, schema, env values, hook Jobs and a drift guard against Kustomize written; **never rendered (no helm binary available)** — structural tests only; CI lint/template step added but not run — see `docs/deployment/HELM.md` |
| Terraform (AWS) | ⚠️ Network, storage/KMS, RDS, Redis, MSK, EKS+IRSA modules and staging/production envs written; `tofu fmt` and policy tests **run**; **never validated, planned or applied (no provider access)** — see `docs/deployment/TERRAFORM.md` |
| CI/CD | ⚠️ CI (+cosign signing, concurrency, timeouts) and a gated staging→production deploy workflow with signature verification, staged migrate-then-rollout, smoke test and auto-rollback; **actionlint passes, smoke test run for real, workflows never run on GitHub** — see `docs/deployment/CICD.md` |
| Disaster recovery | ⚠️ Backup/restore/verify tool and audit-chain verifier **run for real** (drill passed, tamper/corruption cases tested); runbooks for PITR, logical restore, S3, Kafka, Redis, cluster, credentials; **RDS PITR, failover and region loss never exercised, RPO/RTO NOT YET MEASURED, no cross-region protection** — see `docs/disaster-recovery/PHASE_40_DISASTER_RECOVERY.md` |
| Frontend | ✅ React + TypeScript SPA: sign in, **sign up (new organization)**, ask with citations, documents, usage/budget, audit verify, system status; light/dark themes, collapsible sidebar, phone layout; same-origin behind the gateway/ingress with strict CSP; lint, strict typecheck, 88 component tests (incl. automated axe checks), build, nginx config and real-browser E2E **run**; **image never built (Docker Hub unreachable), no manual screen-reader or contrast audit** — see `docs/frontend/PHASE_41_FRONTEND.md` and the [screenshots](docs/screenshots/README.md) |
| Browser E2E | ✅ 17 Playwright/Chromium tests through the real gateway + web nginx configs and the real API: upload → ingest (outbox → relay → consumer) → cited answer, tenant isolation, auth failure/expiry/tamper, RBAC, audit verify, budgets, upload rejection, CSP. **Stand-ins:** loopback Kafka, in-memory object store, extractive LLM, deterministic embeddings — proves wiring, not broker behaviour or answer quality; one browser, no a11y audit; CI job never run — see `docs/testing/PHASE_42_E2E.md` |
| Production readiness | ✅ Executable review (`scripts/readiness_review.py`, 12 checks over both overlays/Dockerfiles/migrations/alerts/secrets, each proven to fail on bad input) found and fixed 3 defects (worker readiness probes, relay PDB, tfvars ignore). **Verdict: NO-GO today** — 11 documented blockers (images/Terraform/Helm never built or planned, no real Kafka/S3/LLM run, RPO/RTO and performance NOT MEASURED, auth gaps) — see `docs/operations/PHASE_43_PRODUCTION_READINESS.md` |
| Security audit | ✅ pip-audit found 4 advisories (pypdf ×3, certifi) → fixed and pinned; staging/production now refuse plaintext Postgres/Redis (`ssl=require`, `rediss://`; explicit opt-out only); a route-authentication audit test fails CI on any unauthenticated endpoint outside a 3-entry allowlist; STRIDE walk-through with residual risks. **Self-audit only — no penetration test**, no revocation/MFA/RLS/sandboxed parsing — see `docs/security/PHASE_44_SECURITY_AUDIT.md` |
| Performance audit | ✅ `/api/v1/query` now load-tested (platform overhead only; stand-in providers). Found and fixed a **pool deadlock** on the query path (16 concurrent: 1.6 → 31 rps, p50 10 s → 0.45 s, silent audit loss gone) and memoised the ACL predicate (+28–38% throughput, clean A/B, with a cache-key leak test). Phase 33 numbers re-run; health paths 16–25% slower, **unexplained**. **Real LLM/embedding latency, Kafka, S3, real hardware NOT MEASURED** — see `docs/performance/PHASE_45_PERFORMANCE_AUDIT.md` |
| Architecture audit | ✅ Import-graph audit found **a five-unit import cycle** (domain↔database↔auth/chunking/parsing) and a citations↔generation cycle; fixed by moving shared value types down, deleted 7 empty Phase-0 scaffold packages, wrote the missing ADRs (001, 003, 004, 007, 009). Layering and no-cycles now **enforced by a test and a CI step**. Boundary checks are unit-granular; no RLS backstop — see `docs/architecture/PHASE_46_ARCHITECTURE_AS_BUILT.md` |
| Documentation | ✅ `docs/README.md` index (every document must be listed), generated **configuration and API references** (checked against code by a test), symptom-first **troubleshooting guide** (every error code and metric it names is verified to exist), `tests/README.md`, `CONTRIBUTING.md`, `SECURITY.md`; fixed 2 stale statements and a missing file referenced by CI. **No security contact address is published (owner must add one)**; interview/system-design docs not yet written (Phase 48) |
| Interview & portfolio | ✅ `docs/interview/` (portfolio summary with evidence table and supportable resume bullets, interview guide with 7 evidence-backed stories and a "what not to say" list, 5-minute demo script) and `docs/system-design/` (45-minute walkthrough, measured figures labelled). Headline numbers are tested against stored result files. States **no-go for production** and that nothing ran against real Kafka/S3/LLM/cluster |
| Dependency management (uv) | ✅ Migrated from pip to **uv**: `uv.lock` (hash-recording, Linux amd64+arm64) is the only lock; `pyproject.toml` dev group; Makefile, CI, Docker build and docs use `uv sync --frozen` / `uv run`. Full suite (backend + 18 browser tests) passes through `uv run`; the Dockerfile's exact sync command was run into a production-only venv (no dev packages, app imports); pip-audit on the export is clean. Migration exposed 2 undeclared test deps (fixed). **Docker image still never built; workflows never run on GitHub** — see ADR-010 |
| Synthetic dataset | ✅ deterministic, idempotent seeding — see `docs/data/PHASE_6_SYNTHETIC_DATASET.md` |
| Kubernetes manifests | ✅ kustomize base + staging/production overlays (hardened pods, probes, HPA, PDB, default-deny NetworkPolicy, migrate/topics jobs, ServiceMonitor, generated PrometheusRule). `kustomize build` + `kubeconform -strict` pass; **never applied to a cluster**; 3 operator CRDs not schema-checked |
| Environment-aware configuration | ✅ `ENVIRONMENT=development` runs with **no Kafka, no Docker**: validated settings (`KAFKA_ENABLED`, empty = unset, `.env` below real env vars), an in-process dispatcher that handles the **durable Postgres outbox** (retries, parking, nothing dropped), local-disk object store, `kafka: "disabled"` in readiness. `production`/`staging` (the default if unset) **require** Kafka, TLS, explicit endpoints and reject every development substitute. Tested (policy, dispatcher vs real Postgres, lifespan with Kafka clients tripwired); the exact dev command was run end to end. **Not run on Windows itself; real broker not exercised** — see ADR-011 and `HOW_TO_RUN.md` |

## Documentation

- [`HOW_TO_RUN.md`](HOW_TO_RUN.md) — run for development and production
- [`docs/README.md`](docs/README.md) — full index (architecture, ADRs, security, deploy, operate, reference)
- `docs/reference/CONFIGURATION.md`, `docs/reference/API.md` — generated from the code and checked in CI
- `docs/troubleshooting/TROUBLESHOOTING.md` — symptom-first fixes
