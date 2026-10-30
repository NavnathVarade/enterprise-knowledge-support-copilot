# Phase 34 — Resilience and chaos testing (Spec §122)

All fault runs below used **real** faults against a real API process (`scripts/chaos_run.py`):
`service postgresql stop`, `redis-cli shutdown`, an `ACCESS EXCLUSIVE` table lock held by a second
connection, SIGTERM/SIGKILL of uvicorn, and a TCP listener that accepts connections and never
answers (a hung dependency). Raw results: `docs/resilience/results/*.json`.

Environment: one disposable sandbox (Postgres 16, Redis, uvicorn with 1 worker, 2 cores shared by
everything), probed at 4 requests/s per endpoint (`/health/live`, `/health/ready`, authenticated
`GET /documents`) plus 1 login/s with a wrong password (expected 401, random unknown user). Single
run per scenario; timings are indicative, not statistically characterised.

## 1. Results: before and after the fixes

| Scenario | Before | After |
|---|---|---|
| **Postgres stopped 10 s** | authenticated requests: **500**; ready 503; live 200; recovered in 2.3 s | authenticated + login: **503 + Retry-After**; ready 503; live 200; recovered in 2.3 s |
| **Redis stopped 10 s** | **ready 503 for the whole outage** (every pod would leave rotation) although all features still worked | ready **200 "degraded"**; documents and login unaffected |
| **Table lock 20 s** (`users`, `documents`) | a request hung 15 s then **500** (login had no timeout at all) | 15 s wait (the configured `statement_timeout`) then **504 + Retry-After**; login bounded too; live/ready unaffected |
| **Database hung** (accepts TCP, never answers) | **ready hung 45 s+** (probe timeout), login unbounded | ready **503 in 3.0 s**, login **504 in 5.0 s** (connect timeout), live always 200 |
| **Redis hung** (accepts TCP, never answers) | each login **4.3 s** (two 2 s socket timeouts) though the throttle fails open | breaker opens after the first failure: logins served at normal speed (max 2.3 s only on the one retry per 10 s cooldown) |
| **API SIGTERM then restart** | connection refused for ~4 s | same (see §3) |
| **API SIGKILL then restart** | connection refused for ~4 s | same |

Provenance: the first run (`chaos_before_fixes.json`) predates the fixes and had no Redis-hung or
login probe. The second run (`chaos_after_fixes.json`) is after the database/readiness fixes
and is also the "before" for the Redis-hung row (login 4.3 s, no breaker yet); the third
(`chaos_redis_breaker.json`) is after the breaker. In the first run the "database hung"
scenario also had an invalid authenticated probe (no token), which is why only `ready` and
`live` are compared for it.

## 2. What changed

1. **Database timeouts on every connection** (`packages/database/session.py`): connect timeout
   (`DATABASE_CONNECT_TIMEOUT_SECONDS`, default 5 s; asyncpg's own default is 60 s), server-side
   `statement_timeout` as a *session default* (previously only set inside tenant-scoped sessions,
   so login and other unscoped sessions could wait forever), and asyncpg `command_timeout` =
   statement timeout + 5 s as a client-side backstop for a peer that goes silent.
2. **Dependency failures are retryable, not 500** (`apps/api/app/main.py`): connection errors,
   SQLAlchemy `OperationalError`/`InterfaceError` and pool exhaustion → **503** with
   `Retry-After: 1`; `TimeoutError` → **504**; a Postgres `query_canceled` (57014) → **504
   `DATABASE_TIMEOUT`**. Integrity/data/programming errors stay 500. Exception text is never sent
   to clients and only the class is logged (connection errors can carry hosts and URLs).
3. **Readiness** (`HEALTH_*`): each dependency check is bounded (`HEALTH_CHECK_TIMEOUT_SECONDS`,
   3 s) and they run concurrently. Redis only gates readiness when it is genuinely required:
   by default only if the login throttle is configured to **fail closed**
   (`HEALTH_REDIS_REQUIRED` overrides). Otherwise a Redis outage reports `degraded` with HTTP 200.
   The database always gates readiness. Liveness never looks at dependencies.
4. **Redis circuit breaker** (`packages/redis/breaker.py`) for the login throttle and the LLM rate
   limiter: after one failure, Redis is skipped for 10 s and the configured fail-open/closed
   policy applies immediately. Per process, not shared across replicas.

## 3. Behaviour that is intentionally unchanged, and why

- **Single-instance restart is a ~4 s outage**, graceful or not. Zero-downtime relies on
  multiple replicas, the rolling-update settings (`maxUnavailable: 0`) and readiness gating in
  the Kubernetes manifests, none of which could be exercised here. Whether in-flight requests
  finish during SIGTERM was not isolated; the probe saw refused connections, not truncated responses.
- **A locked table still costs one `statement_timeout` (15 s) per request** before the 504. It is
  bounded and no longer a 500, but 15 s is long for an interactive call; consider a lower
  `DATABASE_STATEMENT_TIMEOUT_MS` once real query times are known (the hybrid retrieval queries
  measured in Phase 33 take well under 1 s).
- **Postgres outage fails the request**, there is no read cache or queueing. Recovery needs only
  the pool's pre-ping (2.3 s after restart here).
- **`Retry-After` is a fixed 1 s**, not a computed backoff.

## 4. Coverage of the spec's fault list (§122)

| Fault | Status |
|---|---|
| stop Redis | **Real fault run** (stopped and hung); fail-open and fail-closed paths unit/integration tested |
| restart API | **Real fault run** (SIGTERM and SIGKILL) |
| slow database | **Real fault run** (table lock) plus statement-timeout test against a real slow query |
| network failure / hung dependency | **Real fault run** (black-hole listener for Postgres and Redis); packet loss/latency (tc/netem) **not run** |
| Postgres down | **Real fault run** |
| LLM timeout, embedding timeout | Gateway timeout/retry/fallback covered by existing unit tests with scripted providers (`test_llm_gateway.py`, `test_anthropic_provider.py`); **not exercised against a live provider** |
| duplicate event, malformed event | Covered by existing integration tests with real Redis idempotency and a fake broker (`test_kafka_consumer_decision_logic.py`, `test_consumer_runner.py`, `test_ingestion_pipeline.py`); **no real Kafka broker** |
| kill worker, stop Kafka consumer | **Not run** — no broker and no worker containers here. The at-least-once design (commit only after handling or a delivered DLQ record; outbox rows kept until acknowledged) is covered by tests of the decision logic, not by killing a process |
| Kubernetes pod kill / node loss / PDB behaviour | **Not run** — no cluster |
| Disk full, memory pressure, clock skew | **Not run** |

## 5. Tests added

`tests/resilience/test_dependency_failures.py` (12): 503/504/500 mapping including a real Postgres
57014, no exception text leaks, statement timeout present on pooled connections, readiness
degraded/ready/not-ready matrix, hung-dependency readiness bounded and concurrent, liveness
independent of dependencies. `tests/unit/test_redis_breaker.py` (5): breaker timing with a fake
clock, throttle and rate limiter skip a hung Redis, fail-closed refuses immediately, recovery
closes the breaker. `tests/conftest.py` resets the process-wide breakers between tests.

## 6. Not verified / limitations

- No multi-replica, cluster or rolling-update fault testing; no Kafka or worker faults against a real broker.
- Single run per scenario at low probe rate (4 req/s); behaviour under load during a fault (pool exhaustion, retry storms from `Retry-After` clients) was not run.
- Breaker state is per process: with N replicas, each pays one slow request per cooldown.
- Postgres failover, replication lag and Redis failover (Sentinel/Cluster) are deployment concerns not tested.
- Backup/restore and disaster recovery are Phase 40.

Full suite: **797 passed**; ruff, mypy strict and bandit clean; migrations upgrade → downgrade →
upgrade on a fresh database (pgvector 0.8.0 in the sandbox).
