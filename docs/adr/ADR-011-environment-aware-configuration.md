# ADR-011: Environment-aware configuration — development without Kafka, production with it

## Context
The platform's production topology needs Kafka (outbox relay, ingestion consumer, DLQ), S3 and TLS to every dependency. A contributor on a Windows laptop has none of these and cannot run Docker. Until now `KAFKA_BOOTSTRAP_SERVERS` was a required setting, so the API could not even start without a broker address, although the API process never talks to Kafka: it only writes `document.ingested` to the Postgres `outbox_events` table in the same transaction as the upload (ADR-004). Only the two workers use Kafka.

Two bad fixes were available: make Kafka "optional" everywhere (production could then silently run without it) or fake a broker with an in-memory event bus (events lost on restart, uploads stuck `pending`).

## Decision
**One validated settings model, two explicit modes.** `ENVIRONMENT` selects the mode; derived switches live on `Settings` and nothing else inspects the environment name.

| | development | staging / production |
|---|---|---|
| `ENVIRONMENT` default if unset | n/a — **the default is `production`**, so a forgotten variable is stricter, never looser | |
| Kafka (`KAFKA_ENABLED`) | off unless set to `true` | on; an explicit `false` is **rejected** |
| `KAFKA_BOOTSTRAP_SERVERS` | not needed while off (never resolved) | required, non-empty, no localhost, SASL or TLS |
| Event handling | in-process **local dispatcher** reads the Postgres outbox | relay → Kafka → consumer group |
| Redis URL | defaults to `redis://localhost:6379/0` | must be set explicitly (`rediss://`, no localhost) |
| Uploaded originals | S3 bucket, or local disk (`OBJECT_STORE_LOCAL_DIRECTORY`, default `.local-data/object-store`) | S3 bucket required; local disk **rejected** |
| Stale production `.env` | `*.svc.cluster.local` hosts are rejected with an explanatory error | n/a |
| `EVENTS_LOCAL_DISPATCH` | on by default when Kafka is off | **rejected** |

Precedence, highest first: explicit process environment variables → the `.env` file (path overridable with `EKC_ENV_FILE`; empty disables it) → field defaults. A real variable therefore always beats the file, so a stray `.env` cannot downgrade a deployment: `ENVIRONMENT=development` in a file loses to `ENVIRONMENT=production` in the environment, and a file saying `KAFKA_ENABLED=false` makes production fail to start rather than run without Kafka. Empty values (`FOO=`) count as unset for every setting; an empty string is never a configured endpoint.

Settings errors are raised as `SettingsLoadError`, which names the setting and the rule but never a value (DSNs carry passwords; pydantic's default message echoes them).

**Kafka clients are only built on paths that run with Kafka enabled.** Importing a module creates no client. The API never constructs one. The Kafka workers refuse to start when `KAFKA_ENABLED` is false and, when enabled, check broker reachability before consuming so a rollout fails visibly. `KafkaSettings.require_bootstrap_servers()` is the single way a client config is built.

**The local dispatcher** (`packages/workers/local_dispatch.py`) is a deliberately small, honest substitute for the relay + consumer, not an in-memory event bus:

- It reads the same `outbox_events` rows (`FOR UPDATE SKIP LOCKED`, one transaction per event) and calls the same `handle_document_ingested` pipeline.
- **Durable**: the event is a committed row; restarting the API loses nothing.
- **At-least-once**: a row is marked published only after the handler returns; the ingestion handler is idempotent on document state.
- **Nothing is dropped**: failures stay in the table with `attempts` and `last_error`; retryable errors back off exponentially up to `EVENTS_LOCAL_DISPATCH_MAX_ATTEMPTS`; permanent errors are parked immediately. Events with no local handler are left untouched (a later Kafka relay would still publish them).
- **Not provided**: cross-document ordering, replay, consumer groups, a DLQ topic, delivery to other processes, exactly-once. The settings forbid running it together with Kafka (events would be handled twice) and forbid it entirely in staging/production.

**Readiness** reports `kafka: "disabled"` when off. When on, the API probes the broker; because the API only writes the outbox, an outage is `degraded` by default (the outbox absorbs the writes) and fails readiness only when `HEALTH_KAFKA_REQUIRED=true`. Workers fail at startup instead. Redis behaviour is unchanged: an outage is `degraded` and every Redis-backed protection keeps its documented policy (login throttle fail-open is a logged, counted, explicit setting — `HARDENING_LOGIN_THROTTLE_FAIL_OPEN=false` makes it fail closed). No in-process rate limiter pretends to be distributed.

**Renames: none.** The read-only analytics DSN stays `DATABASE_READ_ONLY_ROLE_URL`. New variables: `KAFKA_ENABLED`, `EVENTS_LOCAL_DISPATCH*`, `OBJECT_STORE_LOCAL_DIRECTORY`, `HEALTH_KAFKA_REQUIRED`, `EKC_ENV_FILE`. `.env.example` is now the development template; the production template is `.env.production.example`.

## Alternatives considered
- Treat Kafka as optional in every environment: production could silently lose its event path. Rejected.
- In-memory event bus in development: restart loses events, uploads stay `pending`, and it would be mistaken for Kafka semantics. Rejected in favour of reading the durable outbox.
- Run the dispatcher as a separate process: more terminals for a laptop; the in-API task is one command and shuts down with the app. (Several uvicorn workers would still be safe: `SKIP LOCKED`.)
- SQLite or an embedded Postgres for development: the pgvector/ACL SQL is the product; testing it on another engine proves nothing.

## Consequences
- `uv run uvicorn apps.api.app.main:app --reload --reload-dir apps` is a complete development stack given PostgreSQL (with pgvector) and Redis.
- The Compose reference stack runs `ENVIRONMENT=development` *with* a broker, so it now sets `KAFKA_ENABLED=true`, `EVENTS_LOCAL_DISPATCH=false` and an empty `OBJECT_STORE_LOCAL_DIRECTORY` explicitly (otherwise a developer's own `.env` could leak in).
- Development without an LLM key still runs: ingestion uses the deterministic (non-semantic) embedding stand-in and `/api/v1/query` returns a configuration error. Real answers need `LLM_API_KEY`; real retrieval quality needs `EMBEDDING_API_KEY`.
- `apps.api` lazily imports `packages.workers` (only when the dispatcher is enabled); recorded in the architecture fitness test.
- **Verified:** settings policy (`tests/unit/test_environment_modes.py`), dispatcher mechanics against real Postgres, API lifespan with Kafka client classes replaced by tripwires and `getaddrinfo` spied (no Kafka construction, no broker DNS lookup), readiness per mode, a manual run of the exact development command with only a generated `.env`, and the full suite. **Found and fixed by the live production-mode run:** Alembic failed on the `?ssl=require` DSN that production *requires* (psycopg only knows `sslmode`); `to_sync_database_url` now translates it, and migrations were run up/down/up over real TLS. **Not verified:** a real broker with the new reachability checks, Windows itself (everything was run on Linux), and PostgreSQL/pgvector/Redis installation on Windows.

## Status
Accepted — Phase 50.
