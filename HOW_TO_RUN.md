# How to run Enterprise Knowledge Copilot

One file, in the order you should run things: **Part A** runs the whole product on your own
computer for development (no Docker, no Kafka). **Part B** is the production path (Kafka and TLS
required). **Part C** is the quality-gate command list. **Part D** is a cheat sheet.

> **What was verified.** Every command in Part A was run on Linux with PostgreSQL 16 + pgvector 0.8
> and Redis; the exact `uv run uvicorn ... --reload` command was started, a document was uploaded
> and ingested with Kafka disabled, and the full test suite (1,071 backend tests) passed.
> **Not run:** anything on Windows itself (PowerShell syntax below follows standard behaviour but was
> not executed there; installing pgvector/Redis on Windows is described, not tested), and all of
> Part B (no Docker, Kubernetes, Kafka, S3 or AWS access in the build environment). Production
> readiness verdict today: **NO-GO** — see `docs/operations/PHASE_43_PRODUCTION_READINESS.md`.

## Which mode am I in?

| | Development (Part A) | Production / staging (Part B) |
|---|---|---|
| `ENVIRONMENT` | `development` | `production` or `staging` (**the default if unset is `production`**) |
| Needs Docker / Kafka | **No / No** | Kubernetes or Compose; **Kafka required** |
| Events (`document.ingested`) | in-process dispatcher reads the Postgres outbox | outbox relay → Kafka → ingestion worker |
| Uploaded files | local disk (`.local-data/object-store`) | S3 bucket |
| Config file | `.env` (generated for you) | real environment variables from a secret manager |
| Dependencies | PostgreSQL + pgvector, Redis | + Kafka (SASL/TLS), S3, TLS to Postgres and Redis |

Why it works this way: `docs/adr/ADR-011-environment-aware-configuration.md`.

---

# Part A — Development on your computer

PowerShell is shown first (Windows). macOS/Linux users: same commands in any shell; where a
command differs, a bash line follows.

## A1. Install the tools (once)

| Tool | Why | Install |
|---|---|---|
| **uv** | Python + dependency manager (installs Python 3.12 for you) | `winget install --id=astral-sh.uv -e` or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **PostgreSQL 16** with the **pgvector** extension | the database (required) | see A3 |
| **Redis ≥ 6** | throttling, rate limits, idempotency | see A4 |
| **Node 22** (only for the web UI) | `apps/web` | `winget install OpenJS.NodeJS.LTS` |

Open a **new** terminal after installing uv, then, from the project folder:

```powershell
uv --version
uv python install 3.12        # the repo pins 3.12 in .python-version; uv fetches it if missing
```

## A2. Install dependencies

```powershell
uv sync --frozen              # creates .venv from uv.lock (exact, hash-checked versions)
```

## A3. PostgreSQL + pgvector

1. Install PostgreSQL 16 (e.g. the EnterpriseDB installer) and make sure the service is running:
   ```powershell
   Get-Service postgresql*          # Status should be Running
   ```
2. **Install pgvector into that server.** The schema stores 1024-dimension embeddings, so the
   `vector` extension is mandatory (it is not part of stock PostgreSQL). Follow the official
   Windows instructions at <https://github.com/pgvector/pgvector#windows> (needs the Visual Studio
   C++ build tools), or use a managed PostgreSQL that ships pgvector and put its URL in
   `DATABASE_URL` (add `?ssl=require` if the provider requires TLS). Version ≥ 0.8 is
   recommended (`docs/performance/PHASE_33_PERFORMANCE.md`).
3. Create the role and database (safe to re-run; it never changes an existing password). This
   needs the PostgreSQL superuser password you chose at install time, and the `.env` from A5:
   do **A5 first**, then:
   ```powershell
   uv run python -m scripts.dev_database --admin-password "<postgres superuser password>"
   ```
   It reads role/password/database name from `DATABASE_URL`, creates what is missing and runs
   `CREATE EXTENSION IF NOT EXISTS vector`. If pgvector is not installed it says so and exits 2.

   Prefer plain SQL? As a superuser in `psql`:
   ```sql
   CREATE ROLE ekc_app LOGIN PASSWORD 'your-local-password';
   CREATE DATABASE ekc OWNER ekc_app;
   \c ekc
   CREATE EXTENSION IF NOT EXISTS vector;
   ```

## A4. Redis

Redis is required for login throttling, rate limits and event de-duplication. If it is down the
API still starts and serves, but readiness reports `redis: degraded` and the protections that use
it follow their documented policy (e.g. login throttling fails open unless
`HARDENING_LOGIN_THROTTLE_FAIL_OPEN=false`). Options without Docker:

- **Memurai** (native Windows, Redis-compatible): <https://www.memurai.com/> — installs as a service on port 6379.
- **WSL2**: `wsl --install`, then in Ubuntu `sudo apt install redis-server && sudo service redis-server start` (reachable from Windows as `localhost:6379`).
- macOS: `brew install redis && brew services start redis` · Linux: `sudo apt install redis-server`.

Check it: `redis-cli ping` → `PONG`.

## A5. Create `.env`

```powershell
uv run python -m scripts.init_dev_env --database-password "your-local-password"
```

This copies `.env.example` to `.env` and generates a fresh JWT signing keypair (never commit
`.env`; it is git-ignored). Special characters in the password are percent-encoded for you.
It refuses to overwrite an existing `.env` (use `--force`). Leave `KAFKA_ENABLED=false` and
`KAFKA_BOOTSTRAP_SERVERS=` empty. The development `.env` looks like this:

```dotenv
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://ekc_app:your-local-password@localhost:5432/ekc
REDIS_URL=redis://localhost:6379/0
KAFKA_ENABLED=false
KAFKA_BOOTSTRAP_SERVERS=
OBJECT_STORE_LOCAL_DIRECTORY=.local-data/object-store
SECURITY_JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY----- ... (generated)"
SECURITY_JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY----- ... (generated)"
SECURITY_JWT_ISSUER=ekc-dev
SECURITY_JWT_AUDIENCE=ekc-api
LLM_API_KEY=
EMBEDDING_API_KEY=
```

Precedence: real environment variables beat `.env`, which beats built-in defaults; an empty
value (`FOO=`) means "not set". To use another file: `$env:EKC_ENV_FILE = "C:\path\my.env"`.
If you copy a production `.env` here by mistake, startup stops with an error naming the
`*.svc.cluster.local` settings (values are never printed).

## A6. Create the database tables (migrations)

```powershell
uv run alembic current        # empty on a brand-new database
uv run alembic heads          # 0007_usage_ledger (head)
uv run alembic upgrade head   # applies all 7 revisions
uv run alembic current        # 0007_usage_ledger (head)
```

The PostgreSQL service must be running and the database must exist (A3) first. Migrations are
never run automatically at application start. Roll back with `uv run alembic downgrade -1`.

## A7. Seed demo data (development only)

```powershell
uv run python -m scripts.seed_database
```

Idempotent. Creates two synthetic tenants (`acme-corp`, `globex-corp`), documents and users with
**publicly documented passwords** — it refuses to run unless `ENVIRONMENT` is `development` or
`test`. Logins (tenant `acme-corp`):

| Email | Password | Role |
|---|---|---|
| `alice@example.com` | `alice-synthetic-pw-1` | engineer |
| `bob@example.com` | `bob-synthetic-pw-1` | hr_admin |
| `admin@example.com` | `admin-synthetic-pw-1` | admin (usage, budgets) |
| `security-auditor@example.com` | `secaudit-synthetic-pw-1` | security_auditor (audit log) |
| `carol@example.com`, `john@example.com` | `carol-synthetic-pw-1`, `john-synthetic-pw-1` | support_agent, finance_admin |

## A8. Start the API

```powershell
uv run uvicorn apps.api.app.main:app --reload --reload-dir apps
```

The first log line (`service_startup`) states the active mode without secrets:
`environment=development kafka_enabled=false local_event_dispatch=true object_store=local-disk`.
Interactive API docs (development only): <http://127.0.0.1:8000/docs>.

## A9. Try the real routes

PowerShell (Windows PowerShell 5.1 or 7; `curl.exe` ships with Windows 10+):

```powershell
$api = "http://127.0.0.1:8000"
Invoke-RestMethod "$api/api/v1/health/live"
Invoke-RestMethod "$api/api/v1/health/ready"       # database ok, redis ok, kafka "disabled"

$body = @{ tenant_slug = "acme-corp"; email = "alice@example.com"; password = "alice-synthetic-pw-1" } | ConvertTo-Json  # gitleaks:allow (public demo password)
$token = (Invoke-RestMethod -Method Post "$api/api/v1/auth/login" -ContentType "application/json" -Body $body).access_token
$auth = @{ Authorization = "Bearer $token" }

Invoke-RestMethod "$api/api/v1/documents" -Headers $auth                       # list (ACL-filtered)
"# Retention`n`nThe retention period is 45 days." | Set-Content notes.md
curl.exe -s -H "Authorization: Bearer $token" -F "file=@notes.md;type=text/markdown" "$api/api/v1/documents"
# -> 202 {"id": "...", "status": "uploaded"}; within seconds the document becomes "active"
Invoke-RestMethod "$api/api/v1/documents" -Headers $auth

Invoke-RestMethod -Method Post "$api/api/v1/query" -Headers $auth -ContentType "application/json" `
  -Body (@{ query = "What is the retention period?" } | ConvertTo-Json)
```

bash equivalent (this is what was executed):

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
  -d '{"tenant_slug":"acme-corp","email":"alice@example.com","password":"alice-synthetic-pw-1"}' | jq -r .access_token)  # gitleaks:allow (public demo password)
curl -s -H "Authorization: Bearer $TOKEN" -F 'file=@notes.md;type=text/markdown' localhost:8000/api/v1/documents
```

**Routes** (all JSON; everything except health and login needs `Authorization: Bearer <token>`):

| Method and path | Purpose |
|---|---|
| `GET /api/v1/health/live`, `GET /api/v1/health/ready` | liveness, readiness (dependency status) |
| `POST /api/v1/auth/login` | `{tenant_slug, email, password}` → access token |
| `POST /api/v1/auth/signup` | `{organization_name, tenant_slug, display_name, email, password}` → creates a **new organization** with you as `admin`, returns a token (see below) |
| `GET /api/v1/documents`, `POST /api/v1/documents` | list / upload (multipart `file`) |
| `POST /api/v1/query` | `{query}` → routed, cited answer |
| `GET /api/v1/usage/summary`, `GET`/`PUT /api/v1/usage/budget` | usage and budgets (admin) |
| `GET /api/v1/audit`, `GET /api/v1/audit/verify` | audit log, hash-chain check (security auditor) |

**Signup.** The web app has a `/signup` page backed by `POST /api/v1/auth/signup`. It is **on in development and
test and off in staging/production** (`403 SIGNUP_DISABLED`); to allow it in a deployment set `SIGNUP_ENABLED=true`
(attempts per IP: `SIGNUP_MAX_PER_IP` per `SIGNUP_WINDOW_SECONDS`, default 5 per hour). Passwords need 10+ characters
with a letter and a digit. It only ever creates a new organization; there are no invitations, email verification or
password reset. Screenshots of every page: `docs/screenshots/README.md`.

Full reference: `docs/reference/API.md`.

**Answers need a key.** With empty `LLM_API_KEY`, `POST /api/v1/query` returns a
`CONFIGURATION_ERROR` — there is deliberately no fake model. Put a provider key in `.env`
(`LLM_API_KEY=...`) for generated answers, and `EMBEDDING_API_KEY=...` for real semantic
retrieval; without it ingestion uses a deterministic stand-in whose vectors carry no meaning
(logged as `embeddings=deterministic-dev`). If you add a key after uploading documents, upload
them again so they are embedded with the real model. Everything else (login, upload, ingestion,
listing, ACLs, usage, audit) works without keys.

## A10. Run the web UI (optional)

In a second terminal (the API must be running on port 8000; the dev server proxies `/api` to it):

```powershell
cd apps\web
npm ci
npm run dev                   # http://localhost:5173 — log in as alice@example.com (tenant acme-corp)
```

## A11. Run the tests

```powershell
uv run pytest tests/unit                         # no services needed for most; fast
```

Integration/resilience/e2e tests need a **separate** database with pgvector and Redis. Create
`ekc_test` as a superuser (the helper script only runs in development mode, so use SQL here),
point at the test configuration, migrate, run:

```sql
-- psql as a superuser
CREATE DATABASE ekc_test;
\c ekc_test
CREATE EXTENSION IF NOT EXISTS vector;
```

```powershell
$env:EKC_ENV_FILE = ".env.test"       # edit DATABASE_URL in it if your superuser/password differ
uv run alembic upgrade head
uv run pytest -q
```

(`.env.test` targets `postgres:postgres@localhost/ekc_test`; the e2e browser tests also need
Chromium: `uv run playwright install chromium`, or `EKC_CHROMIUM_PATH`.) Frontend:
`cd apps\web; npm run lint; npm run typecheck; npm test; npm run build`.

## A12. Troubleshooting (development)

| Symptom | Cause | Fix |
|---|---|---|
| `Invalid configuration ... Settings.database.url: Field required` | no `.env` / `DATABASE_URL` | run A5; run the command from the project folder (`.env` is read from the working directory) |
| `ENVIRONMENT=development but [...] point at cluster-internal hostnames` | a production `.env` was copied | use `.env.example` values or `init_dev_env --force` |
| `KAFKA_BOOTSTRAP_SERVERS is required when Kafka is enabled` | `KAFKA_ENABLED=true` in dev | set `KAFKA_ENABLED=false` (or provide a broker) |
| `KAFKA_ENABLED=false is not permitted in staging/production` | `ENVIRONMENT` is not `development` (it defaults to `production`) | set `ENVIRONMENT=development` in `.env` / the shell |
| `connection refused` on 5432 / `password authentication failed` | PostgreSQL stopped, or wrong password in `DATABASE_URL` | `Get-Service postgresql*`; re-run `scripts.dev_database`; percent-encode special characters |
| `type "vector" does not exist` / migration fails early | pgvector missing or extension not created | install pgvector (A3), re-run `scripts.dev_database` |
| `/api/v1/health/ready` → `redis: degraded` | Redis not running | start it (A4); the API keeps working |
| Upload stays `uploaded` | dispatcher not running | startup log must say `local_event_dispatch=true`; check `EVENTS_LOCAL_DISPATCH` is not `false`; look for `local_event_failed` log lines |
| A document failed ingestion | permanent error (bad file) or retries exhausted | the row is kept: `SELECT id, topic, attempts, last_error FROM outbox_events WHERE published_at IS NULL;` — reset retries with `UPDATE outbox_events SET attempts = 0 WHERE id = '...';` |
| Query → `CONFIGURATION_ERROR` | no `LLM_API_KEY` | add a key (A9) |
| `login` → 429 after typos | login throttle (per account and IP, 15 min) | wait, or clear Redis keys: `redis-cli FLUSHDB` (dev only!) |
| `--reload` does not pick up changes in `packages/` | only `apps` is watched | add `--reload-dir packages --reload-dir rag` |

Reset everything: stop the API, delete `.local-data\`, then in `psql`: `DROP DATABASE ekc;` and redo A3, A6, A7.

---

# Part B — Production (and staging)

Read first: production **requires** Kafka, S3-compatible storage, TLS to Postgres and Redis,
real provider keys and fresh JWT keys. Settings validation refuses to start otherwise (and refuses
every development substitute). Nothing in this part has been executed in the build environment;
`docs/operations/PHASE_43_PRODUCTION_READINESS.md` lists the 11 blockers behind the NO-GO verdict
(images and Terraform never built/planned, no run against real Kafka/S3/LLM, RPO/RTO unmeasured,
auth gaps). Use these steps to *reach* a deployment, not as proof of one.

## B1. Environment variables (secret manager, never a committed file)

Template with every variable: `.env.production.example`. Required:

| Variable | Rule enforced at startup |
|---|---|
| `ENVIRONMENT=production` | only `production`/`staging` give strict mode (unset also means production) |
| `DATABASE_URL` | service DNS host (no localhost), `?ssl=require` (or `verify-ca`/`verify-full`) |
| `DATABASE_READ_ONLY_ROLE_URL` | analytics role, same TLS rule; without it SQL analytics is disabled (by design) |
| `REDIS_URL` | explicit, `rediss://`, no localhost |
| `KAFKA_ENABLED=true` (or unset) | `false` is rejected |
| `KAFKA_BOOTSTRAP_SERVERS` | non-empty, no localhost |
| `KAFKA_SECURITY_PROTOCOL=SASL_SSL` + `KAFKA_SASL_MECHANISM`, `KAFKA_SASL_USERNAME`, `KAFKA_SASL_PASSWORD`, `KAFKA_SSL_CA_LOCATION` | `PLAINTEXT` rejected; SASL needs all three |
| `OBJECT_STORE_BUCKET` (+ `OBJECT_STORE_REGION`, `OBJECT_STORE_SSE`) | required; `OBJECT_STORE_LOCAL_DIRECTORY` rejected |
| `LLM_API_KEY`, `EMBEDDING_API_KEY` | required |
| `SECURITY_JWT_PUBLIC_KEY`, `SECURITY_JWT_PRIVATE_KEY`, `SECURITY_JWT_ISSUER`, `SECURITY_JWT_AUDIENCE` | the committed test key is rejected; generate a new keypair |
| `OTEL_METRICS_BEARER_TOKEN` | unset ⇒ `/metrics` is disabled (404) |
| `HARDENING_TRUSTED_PROXY_HOPS`, `HARDENING_DOCS_ENABLED=false` | set to your proxy chain; docs off |

`EVENTS_LOCAL_DISPATCH` must stay unset/false. AWS credentials come from the workload identity
(IRSA), never variables. Generate a keypair: `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt-private.pem && openssl rsa -in jwt-private.pem -pubout -out jwt-public.pem`.

## B2. Order of operations (any target)

1. **Provision infrastructure** — AWS via `infrastructure/terraform` (B5), or bring your own Postgres 16 + pgvector ≥ 0.8, Redis, Kafka, S3 bucket.
2. **Build and push the images** (CI does this; manual form):
   ```bash
   docker build -f infrastructure/docker/Dockerfile -t ghcr.io/<owner>/ekc:<tag> --build-arg VERSION=<tag> --build-arg GIT_SHA=$(git rev-parse HEAD) .
   docker build -t ghcr.io/<owner>/ekc-web:<tag> apps/web
   docker push ghcr.io/<owner>/ekc:<tag> && docker push ghcr.io/<owner>/ekc-web:<tag>
   ```
3. **Create secrets** (`ekc-secrets`, `ekc-kafka-ca`, `ekc-metrics-token`) in your secret manager / cluster.
4. **Create the analytics role** once (`infrastructure/sql/analytics_readonly_role.sql`; Compose does it via its `db-roles` job).
5. **Migrate**: `alembic upgrade head` (a Job in Kubernetes/Helm/Compose; never at API start). The `vector` extension must already exist (needs a privileged role once: `CREATE EXTENSION vector;`).
6. **Create Kafka topics**: `python -m scripts.create_topics --partitions 12 --replication-factor 3` (a Job; brokers run with auto-create off).
7. **Start** API, `outbox-relay` (one replica) and `ingestion` workers, web.
8. **Verify** (B6).

## B3. Docker Compose (single host reference stack)

```bash
cp .env.compose.example .env.compose        # replace every __SECRET__
cp .env.production.example .env              # JWT keys and provider keys
docker compose --env-file .env.compose -f infrastructure/docker/docker-compose.yml up -d --build
# optional Prometheus + Grafana:
docker compose --env-file .env.compose -f infrastructure/docker/docker-compose.yml \
  -f infrastructure/docker/docker-compose.observability.yml up -d
python -m scripts.smoke_test --base-url http://127.0.0.1:8080
```

This stack runs a single PLAINTEXT Kafka and no TLS, so it is a reference topology
(`ENVIRONMENT=development` with `KAFKA_ENABLED=true` set explicitly), not production
(`docs/deployment/DOCKER.md`). Stop: `docker compose ... down` (add `-v` to delete data).

## B4. Kubernetes (kustomize) or Helm

```bash
# kustomize — images are filled in by CI; delete finished Jobs before re-applying
kubectl delete job -n ekc ekc-migrate ekc-topics-init --ignore-not-found
kubectl apply -k infrastructure/kubernetes/overlays/production        # or overlays/staging

# Helm (hook Jobs migrate and create topics before the rollout)
helm upgrade --install ekc infrastructure/helm/ekc -n ekc --create-namespace \
  -f infrastructure/helm/ekc/values-production.yaml \
  --set image.repository=ghcr.io/<owner>/ekc --set image.tag=<tag> --set ingress.host=<your-host>

kubectl -n ekc rollout status deploy/ekc-api
```

Label the namespace `pod-security.kubernetes.io/enforce=restricted`. Details, probes, PDBs and
network policies: `docs/deployment/KUBERNETES.md`, `docs/deployment/HELM.md`. CI/CD pipeline
(signing, staged migrate-then-rollout, smoke test, rollback): `docs/deployment/CICD.md`.

## B5. Terraform / OpenTofu (AWS)

```bash
cd infrastructure/terraform/envs/production        # or envs/staging
cp backend.hcl.example backend.hcl && cp terraform.tfvars.example terraform.tfvars    # edit; both git-ignored
tofu init -backend-config=backend.hcl
tofu plan -out plan.bin && tofu apply plan.bin
```

Create the state bucket and lock table first. The modules were formatted and policy-tested but
never planned or applied (`docs/deployment/TERRAFORM.md`).

## B6. Verify it is really production

```bash
python -m scripts.smoke_test --base-url https://<your-host> --production
curl -s https://<your-host>/api/v1/health/ready        # {"status":"ready","checks":{"database":"ok","redis":"ok","kafka":"ok"}}
kubectl -n ekc logs deploy/ekc-api | grep service_startup
```

- The startup line must show `"environment": "production"`, `"kafka_enabled": true`, `"local_event_dispatch": false`, `"object_store": "s3"`.
- `kafka: "disabled"` in readiness, `local_event_dispatch: true` or `object_store: "local-disk"` in production is impossible by construction: the process refuses to start. If you ever see them, you are not looking at production.
- `kafka: "degraded: ..."` means the broker is unreachable from the API; uploads still commit to the outbox and drain when Kafka returns (alert `EkcOutboxBacklogAging`). Workers exit at start-up if Kafka is unreachable. Set `HEALTH_KAFKA_REQUIRED=true` if you want the API itself to fail readiness.
- `/docs` must be 404, `/metrics` must be 404 on the public edge.
- Dashboards/alerts: `docs/operations/PHASE_29_OBSERVABILITY.md`. Backups and restore drills: `docs/disaster-recovery/PHASE_40_DISASTER_RECOVERY.md`. Symptom-first fixes: `docs/troubleshooting/TROUBLESHOOTING.md`.

## B7. Graceful shutdown, retries, rollback

- API: SIGTERM, 25 s graceful window; workers finish the in-flight message (up to 120 s). The development dispatcher is stopped the same way.
- Kafka delivery is at-least-once through the outbox; consumers are idempotent; poison messages go to `*.dlq`. Retry/backoff numbers: `docs/kafka/PHASE_8_KAFKA_DESIGN.md`.
- Rollback: `helm rollback ekc <revision>` or redeploy the previous signed image tag; downgrade migrations only after reading the revision (`alembic downgrade -1`).

---

# Part C — Quality gates (what CI runs, locally)

```bash
uv lock --check                                  # lock matches pyproject
uv run ruff check . && uv run ruff format --check .
uv run mypy packages apps/api/app data scripts rag
uv run pytest                                    # needs Postgres + pgvector + Redis (see A11)
uv run python -m scripts.architecture_check      # layering / no import cycles
uv run python -m scripts.readiness_review        # 12 production-readiness checks
uv run python -m scripts.gen_reference_docs --check   # docs/reference matches the code
uvx --from "bandit[toml]" bandit -q -r apps packages rag scripts -c pyproject.toml
uv export --frozen --no-dev --no-emit-project --no-hashes -o prod-requirements.txt && uvx pip-audit -r prod-requirements.txt --no-deps --disable-pip
gitleaks detect --no-git -s .
make check                                       # lock-check + lint + format + typecheck + tests
```

# Part D — Cheat sheet

| I want to… | Command |
|---|---|
| install deps | `uv sync --frozen` |
| make `.env` | `uv run python -m scripts.init_dev_env --database-password "<pw>"` |
| create dev role + DB + pgvector | `uv run python -m scripts.dev_database --admin-password "<superuser pw>"` |
| migrate | `uv run alembic upgrade head` (`current`, `heads`, `downgrade -1`) |
| seed demo data (dev only) | `uv run python -m scripts.seed_database` |
| run the API (dev) | `uv run uvicorn apps.api.app.main:app --reload --reload-dir apps` |
| run the web UI (dev) | `cd apps\web; npm ci; npm run dev` |
| run Kafka workers (prod / dev with a broker) | `uv run python -m packages.workers outbox-relay` · `uv run python -m packages.workers ingestion` |
| create Kafka topics | `uv run python -m scripts.create_topics` |
| unit tests | `uv run pytest tests/unit` |
| all backend tests | `$env:EKC_ENV_FILE=".env.test"; uv run pytest` |
| evaluations | `make eval-routing eval-retrieval eval-answer` |
| smoke-test a deployment | `uv run python -m scripts.smoke_test --base-url <url> [--production]` |

Where to read more: `README.md` (overview and verified status), `docs/README.md` (index),
`docs/reference/CONFIGURATION.md` (every variable), `docs/reference/API.md` (every route).
