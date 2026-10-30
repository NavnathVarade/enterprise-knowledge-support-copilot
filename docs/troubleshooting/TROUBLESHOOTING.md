# Troubleshooting guide

Symptom-first. Every metric, alert, error code and message named here exists in the code; where a
step needs something this repository cannot show (cluster state, provider consoles) it says so.
Alert-specific procedures live in `docs/operations/PHASE_29_OBSERVABILITY.md` (one heading per alert).
First move for any user report: get the `request_id` from the error body (`{"error": {"code", "message", "request_id"}}`) and search the API logs for it; the same id appears in traces.

## 1. The service will not start

| Message (startup validation) | Cause | Fix |
|---|---|---|
| `KAFKA_ENABLED=false is not permitted in staging/production` | Production/staging (the default when `ENVIRONMENT` is unset) with Kafka switched off | Set `KAFKA_ENABLED=true` and brokers; for a laptop set `ENVIRONMENT=development` (see `HOW_TO_RUN.md`) |
| `KAFKA_BOOTSTRAP_SERVERS is required when Kafka is enabled` | Kafka on but no (or empty) broker list | Set the brokers; in development set `KAFKA_ENABLED=false` |
| `REDIS_URL must be set explicitly in staging/production` | Production without a Redis URL (the localhost default is development-only) | Set `REDIS_URL=rediss://...` |
| `EVENTS_LOCAL_DISPATCH ... not permitted` / `OBJECT_STORE_LOCAL_DIRECTORY is development-only` | A development substitute configured in staging/production | Remove it; use Kafka and `OBJECT_STORE_BUCKET` |
| `ENVIRONMENT=development but [...] point at cluster-internal hostnames` | A production `.env` copied to a developer machine | Use `.env.example` values |
| `The 'ingestion' worker ... cannot run with Kafka disabled` | A Kafka worker started with `KAFKA_ENABLED=false` | Enable Kafka; in development ingestion runs inside the API |
| `invalid connection option "ssl"` from `alembic` | Alembic older than Phase 50 given a production `?ssl=require` DSN | Use the current code: `migrations/env.py` translates `ssl` to `sslmode` |
| `Localhost-style hosts are not permitted for inter-service communication ...` | `DATABASE_URL`, `REDIS_URL` or `KAFKA_BOOTSTRAP_SERVERS` points at localhost/127.0.0.1/0.0.0.0 with `ENVIRONMENT` staging/production | Use Compose service names or Kubernetes DNS |
| `Unencrypted dependency connections are not permitted ...` | Postgres DSN lacks `?ssl=require` (or `verify-ca`/`verify-full`) or Redis URL is not `rediss://` | Fix the URLs. Only on a trusted private network, set `HARDENING_REQUIRE_DEPENDENCY_TLS=false` deliberately |
| `KAFKA_SECURITY_PROTOCOL=PLAINTEXT is not permitted ...` / SASL variables missing | Kafka not configured for TLS/SASL | Set `SASL_SSL` plus mechanism, username, password (secret manager) |
| `SECURITY_JWT_PUBLIC_KEY is the test key committed to this repository` | The public test key is configured | Generate a new keypair; the private half of the committed key is public |
| Validation error naming `LLM_API_KEY`, `EMBEDDING_API_KEY` or `OBJECT_STORE_BUCKET` | Provider settings missing outside dev/test | Provide them; see `docs/reference/CONFIGURATION.md` |
| Pods restart in a loop, `startupProbe` failing (workers) | Worker never wrote its first heartbeat: cannot reach Kafka/Postgres, or bad credentials | Check worker logs; startup allows about 3 minutes before liveness takes over |

## 2. Users cannot sign in

| Symptom | Likely cause | Check |
|---|---|---|
| 401 `Invalid tenant, email, or password.` | Wrong credentials, or unknown tenant (the message is intentionally identical for both) | Confirm the tenant slug and user exist; the failure is audited for known tenants only |
| 429 `LOGIN_THROTTLED` | Too many failures from one IP or account | `ekc_login_throttled_total`; wait for the window, check for credential stuffing |
| Login works but throttle never engages | Redis unreachable (throttle fails open and counts) | `ekc_login_throttle_errors_total`, Redis health |
| 503 `SERVICE_BUSY` with `Retry-After: 1` | Database pool exhausted, typically a burst of logins (bcrypt is CPU-bound, about 3 logins/s per core, measured in Phase 33) | Scale API replicas; clients should honour `Retry-After` |
| Signed out unexpectedly | Token expired (no refresh tokens: every expiry is a full login) or the server rejected the token (`INVALID_TOKEN`) | Check token lifetime, issuer/audience/public key match across replicas |

## 3. Uploads and ingestion

Document states: `uploaded` → `validating` → `processing` → `parsed` → `chunked` → `embedding` → `indexing` → `active` (shown as "Ready" in the UI), or `processing_failed` / `embedding_failed` / `indexing_failed` / `quarantined`.

| Symptom | Likely cause | Check / fix |
|---|---|---|
| Stays `uploaded` (development) | Local dispatcher not running (startup log must show `local_event_dispatch=true`) or the event failed: `SELECT attempts, last_error FROM outbox_events WHERE published_at IS NULL` | Fix the cause; `UPDATE outbox_events SET attempts = 0 WHERE id = ...` to retry |
| Stays `uploaded` (production) | Outbox relay or ingestion worker not running or not connected | `ekc_outbox_unpublished_events`, `ekc_outbox_oldest_unpublished_age_seconds` (alert `EkcOutboxBacklogAging`), `ekc_outbox_relay_events_total`; relay and worker pod logs; Kafka reachability |
| Stuck mid-pipeline | Consumer lag or handler retries | `ekc_kafka_consumer_lag_messages` (`EkcKafkaConsumerLagHigh`), `ekc_kafka_handler_retries_total` |
| `processing_failed` | Permanent failure (empty text, corrupt file); not retried | `ekc_ingestion_documents_total` by outcome (`EkcIngestionPermanentFailures`); `PARSING_FAILED`/`INVALID_DOCUMENT` in logs |
| `embedding_failed` | Embedding provider down or rejecting | `EMBEDDING_PROVIDER_UNAVAILABLE`; provider status; reprocess after recovery |
| `quarantined` | Content screening flagged the document | Review the document; `ekc_injection_chunks_quarantined_total` |
| Messages in `*.dlq` topics | Poison messages after retries (`DocumentNotFound`/`ObjectNotFound` are dead-lettered immediately) | `EkcDeadLetteredMessages`; inspect the message, fix the cause, replay deliberately (no replay tool is provided) |
| 413 `REQUEST_TOO_LARGE` / `PAYLOAD_TOO_LARGE`, `UNSUPPORTED_MIME_TYPE`, or 429 on upload | Size cap, type not allowed, or the per-minute upload limit | `HARDENING_MAX_REQUEST_BODY_BYTES`, `INGESTION_*`, `RATE_LIMIT_INGESTION_UPLOADS_PER_MINUTE` |
| Reading a PDF fails or hangs | Parsers run in-process (known limitation) | Worker CPU/memory; consider the file hostile |

## 4. Answers look wrong

| Symptom | Likely cause | Check |
|---|---|---|
| "ABSTAINED / not enough evidence" for a question the documents answer | No authorised chunk matched, documents not `active`, wrong tenant, or ACL excludes the user (abstention is the safe default) | Document status and ACL; `ekc_answer_abstentions_total` (`EkcAbstentionRateHigh`); the response `trace` (`retrieved_candidates`, `context_chunks`, `dropped_*`) |
| Far fewer results than expected for a small tenant | HNSW filtered-recall limit on pgvector < 0.8 (Phase 33) | Run pgvector >= 0.8; see ADR-001 and `RETRIEVAL_HNSW_*` |
| "not supported / Analytics is not enabled" | Question routed to SQL analytics but no read-only DSN is configured, or the intent is outside the template catalogue | `DATABASE_READ_ONLY_ROLE_URL`; the response `route.reason` shows why it was routed |
| Question routed to the wrong path | Rule-based router (`rules-v1`) mis-scored a phrase (for example count-like words route to analytics) | `route.reason` and `route.confidence` in the response |
| `CITATION_VALIDATION_FAILED`, `OUTPUT_POLICY_VIOLATION` | The model cited a source it was not given, or output was blocked by policy | `ekc_citation_validation_failures_total`, `ekc_output_policy_violations_total` |
| `LLM_TIMEOUT` / `LLM_UNAVAILABLE` | Provider slow or down; breaker open | `ekc_llm_requests_total`, `ekc_llm_retries_total` (`EkcLlmFailureRatioHigh`); fallback model if configured |
| 429 `BUDGET_EXCEEDED` / `RATE_LIMIT_EXCEEDED` | Daily token/cost budget or per-minute query limit | `ekc_budget_rejections_total`; Usage page or `GET/PUT /api/v1/usage/budget`; `RATE_LIMIT_*` |

## 5. Latency and capacity
Measured limits (Phase 45, stand-in providers, 2 shared cores): one API worker saturates near 30 queries/s of platform work; latency then rises linearly with concurrency. `ekc_http_requests_in_flight` and `ekc_query_duration_seconds` show saturation; scale API replicas (HPA owns replica count) before raising the DB pool. If many requests fail with `SERVICE_BUSY`, the pool is the limit: check `DATABASE_POOL_SIZE`/`DATABASE_MAX_OVERFLOW` against replicas x pool vs Postgres `max_connections`. Real LLM latency is not in these numbers.

## 6. Audit and integrity
`GET /api/v1/audit/verify` (admin or security auditor) re-walks the tenant's hash chain. A failure means a row was edited, deleted or a sequence gap exists: stop, preserve the database, and follow `docs/disaster-recovery/PHASE_40_DISASTER_RECOVERY.md` (tamper section). `ekc_audit_write_failures_total` rising (`EkcAuditWriteFailures`) means login/query audit writes are being dropped (best effort by design); check database health and pool pressure. Upload audit is strict: no audit row, no upload.

## 7. Health and probes
`/api/v1/health/live` only proves the process answers. `/api/v1/health/ready` checks Postgres (failing returns 503 and removes the pod from the Service without restarting it). A Redis outage reports `degraded` with HTTP 200 unless Redis is configured as required (`HEALTH_REDIS_REQUIRED`), so look at the response body, not only the status code (`docs/resilience/PHASE_34_RESILIENCE.md`). Worker probes run `python -m packages.workers.health` and check heartbeat age (900 s default).

## 8. When to escalate
Suspected cross-tenant data exposure, a failing audit verification, or a leaked token/key: treat as a security incident (rotate keys, preserve logs, see `docs/security/PHASE_44_SECURITY_AUDIT.md` for what revocation does not exist yet: a leaked token stays valid until expiry).
