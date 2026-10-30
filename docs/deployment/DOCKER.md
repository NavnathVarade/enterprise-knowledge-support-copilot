# Docker: image and Compose reference environment (Spec §91, §92; Phase 35)

Kubernetes is the production target (`infrastructure/kubernetes`, Phase 30). This document covers
the container image and the single-host Compose environment that reproduces the same topology.

## 1. Topology

```
 client ──► gateway (nginx, :8080, loopback by default)
                │  app-net (can reach the internet: LLM / embedding providers)
                ▼
              api ──────────────┐
 ingestion-worker ──────────────┤  data-net  (internal: NO route out)
 outbox-relay ──────────────────┤   postgres · redis · kafka · minio
                                └── one-shot jobs: migrate → db-roles ; topics-init ; minio-init
 optional: prometheus ◄── scrapes api / workers over metrics-net (internal) + app-net ── grafana
```

| Service | Role | Notes |
|---|---|---|
| `gateway` | only published port | nginx-unprivileged; sets the forwarded-for chain, 404s `/metrics`, edge rate limit on login, 26m body cap, no retry of POSTs, JSON access log without query strings |
| `api` | FastAPI/uvicorn | one process per container; scale by replicas |
| `ingestion-worker`, `outbox-relay` | Kafka consumer / transactional-outbox publisher | same image, different command; heartbeat-file health check |
| `migrate` | `alembic upgrade head` | runs before everything that uses the schema |
| `db-roles` | creates the read-only analytics role | **without it the SQL analytics engine is disabled by design**; idempotent, also rotates the password |
| `topics-init` | creates Kafka topics | brokers run with auto-create off |
| `minio`, `minio-init` | S3-compatible store and its bucket | **the application never creates its bucket**; `minio-init` does |
| `postgres` (pgvector 0.8.0), `redis`, `kafka` (KRaft, single node) | dependencies | pinned tags |
| `prometheus`, `grafana` | `docker-compose.observability.yml` add-on | provisioned datasource and the `ekc-overview` dashboard |

## 2. Running it

```
cp .env.compose.example .env.compose      # replace every __SECRET__
cp .env.production.example .env                    # JWT keys, provider keys (never committed)
docker compose --env-file .env.compose -f infrastructure/docker/docker-compose.yml up -d --build
# with observability:
docker compose --env-file .env.compose -f infrastructure/docker/docker-compose.yml \
  -f infrastructure/docker/docker-compose.observability.yml up -d
```

Compose refuses to start without `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`,
`ANALYTICS_RO_PASSWORD` and `METRICS_TOKEN` (and `GRAFANA_ADMIN_PASSWORD` for the add-on): these
have no defaults on purpose. The API is then at `http://127.0.0.1:8080` (gateway).
Set `GATEWAY_BIND=0.0.0.0` only behind a TLS-terminating load balancer; **this stack does not
terminate TLS.**

> The stack runs `ENVIRONMENT=development` **with** a broker, so the Compose environment sets `KAFKA_ENABLED=true`, `EVENTS_LOCAL_DISPATCH=false` and an empty `OBJECT_STORE_LOCAL_DIRECTORY` explicitly; a developer's own `.env` (which uses the in-process dispatcher) cannot leak into it. The `.env` this stack reads is `.env.production.example`-shaped, not the development template.

## 3. Image properties (requirement checklist)

| Requirement | How |
|---|---|
| multi-stage, minimal | builder stage has compilers; runtime has only `libpq5` and `curl`; venv copied across |
| non-root | numeric uid/gid 10001; read-only root filesystem + `/tmp` tmpfs in Compose |
| health checks | image `HEALTHCHECK` (API), Compose checks per service, worker heartbeat file |
| explicit entrypoint | `ENTRYPOINT ["uvicorn"]`, exec form; roles override command |
| graceful shutdown | `STOPSIGNAL SIGTERM`; uvicorn `--timeout-graceful-shutdown 25`; Compose `stop_grace_period` 30 s (API), 120 s (ingestion worker, finishes the in-flight message) |
| env configuration, no secrets | all via environment; policy tests fail on a secret ENV/ARG or a literal credential in Compose |
| reproducible | exact, hash-verified versions from `uv.lock` (`uv sync --frozen --no-dev`), exact third-party image tags, OCI labels (`VERSION`, `GIT_SHA` build args) |

Compose adds for every application container: `cap_drop: ALL`, `no-new-privileges`, CPU / memory /
pid limits, `json-file` logs capped at 3 × 10 MB, `init: true`. Limits are **untuned starting
values** (NOT YET MEASURED under load).

## 4. What was verified in the build sandbox

| Item | Status |
|---|---|
| `docker compose config` for the base file and base + observability | **Run, valid** |
| 30 static policy tests over the Compose files (pinning, ports, hardening, limits, networks, secrets, ordering, gateway/app agreement) | **Run; each class of regression shown to fail the test** |
| nginx gateway config | **Run for real**: nginx 1.24 in front of a live API. `/metrics` is 404 through the gateway but 200 direct; a forged `X-Forwarded-For` is ignored (the address nginx saw is what the login throttle keys on); request ids minted or preserved; 2 MB JSON → API 413; 28 MB upload → nginx 413; 70-request login burst → 429s at the edge (tests: `test_gateway_nginx.py`) |
| Prometheus config and 13 alert rules | **`promtool` 2.55.1: valid** |
| Prometheus scraping the API with the bearer token | **Run for real**: target `up`, `ekc_build_info` present, wrong token → 401 |
| Analytics role SQL | **Run for real** against Postgres: role created, idempotent re-run, role can login, `SELECT` on `users` denied, `CREATE TABLE` refused (read-only) |
| `docker build` of the changed Dockerfile; hadolint | **Built (against the substitute base image, see Phase 30) and clean** |
| Container started with Compose's hardening flags (read-only, caps dropped, no-new-privileges, pid limit, init) | **Run**: healthy |
| Graceful shutdown | **Run**: 6 logins in flight (~0.3 s CPU each) during `docker stop` all completed (6 × 401); requests that had not yet connected were refused, as expected. The container exits 143 under `--init` (docker-init reports the signal that stopped uvicorn) and 0 without; both are normal for a SIGTERM stop |

## 5. NOT verified (do before relying on this)

- **The full stack was never brought up** (`docker compose up`): Docker Hub is unreachable from the sandbox, so none of these third-party tags were pulled and their existence is **assumed from memory**: `pgvector/pgvector:0.8.0-pg16`, `redis:7.4.1-alpine`, `apache/kafka:3.7.0`, `minio/minio:RELEASE.2024-10-13T13-34-11Z`, `minio/mc:RELEASE.2024-10-08T09-37-26Z`, `nginxinc/nginx-unprivileged:1.27.2-alpine`, `prom/prometheus:v2.55.1`, `grafana/grafana:11.3.1`. Check each with `docker pull`; then pin by digest.
- The MinIO health check (`mc ready local`), the Kafka health check and the `wget`-based gateway health check depend on binaries inside those images; unverified.
- Grafana provisioning files were reviewed, not loaded by Grafana. The dashboard JSON was not rendered.
- Real Kafka consumption, S3 writes and LLM calls from inside the stack.
- The `data-net` being `internal` means those containers cannot pull anything or reach the internet; that is the intent, but Kafka/Postgres images that try to download at start would fail.
- nginx 1.24 (Ubuntu) was tested; the container uses 1.27.2. Directives used are stable across both, but this is not proof.

## 6. Known limitations

- Single-node Kafka on PLAINTEXT is for `ENVIRONMENT=development` only; staging/production settings reject PLAINTEXT and require SASL.
- `ENVIRONMENT=development` in Compose means the OpenAPI docs are enabled (`HARDENING_DOCS_ENABLED` unset); production defaults them off.
- The gateway still answers with `Server: nginx` (version hidden). Removing it needs a module not in the stock image.
- Rate limits at the gateway (30 requests/min login, 50 requests/s general per address) are generous guesses, NOT YET MEASURED.
- No secrets manager: secrets arrive through environment variables / a local `.env` file. Use the Kubernetes ExternalSecret path for anything real.
- Images are not signed and there is no digest pinning for the Python base image yet.

## 7. Tests

`tests/unit/test_compose_assets.py` (30), `tests/integration/test_gateway_nginx.py` (6, skipped
when the `nginx` binary is absent; CI installs it). Full suite at the end of this phase:
**833 passed**; ruff, mypy strict, bandit and gitleaks clean; migrations upgrade → downgrade →
upgrade on a fresh database.
