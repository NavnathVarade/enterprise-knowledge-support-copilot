# Phase 22 — Safe SQL Analytics Layer (Spec §39–41)

The model never writes SQL and never sees credentials. Flow:
`question → (template | LLM → JSON intent) → strict parse → validated builder → read-only executor`.

## Layers of protection (each verified against real Postgres unless noted)
| # | Protection | Where |
|---|---|---|
| 1 | Semantic layer allow-list: only registered entities/columns/functions/operators/buckets exist | `semantic_layer.py` |
| 2 | Strict intent parser: unknown keys rejected (no smuggled SQL/tables/joins) | `intent.py` |
| 3 | Builder emits one SELECT from metadata only; all values bound; types and enum values checked; no joins/subqueries | `builder.py` |
| 4 | `tenant_id = :tenant` always applied from the authenticated tenant, never from the intent | `builder.py` |
| 5 | Row limit capped (≤1000), limit+1 fetched, `truncated` reported | `builder.py`, `executor.py` |
| 6 | READ ONLY transaction + per-transaction `statement_timeout` → `AnalyticsQueryTimeout` | `executor.py` |
| 7 | **Row-level security** on all six tables, keyed on `app.current_tenant_id`; unset ⇒ zero rows (fail closed) | migration `0004` |
| 8 | **Dedicated read-only role** `ekc_analytics_ro`: SELECT on the six analytics tables only; default read-only; timeouts; connection limit | `infrastructure/sql/analytics_readonly_role.sql` |

Layers 7–8 hold even if layers 3–4 had a bug: a test runs a deliberately **unfiltered** statement and each tenant still sees only its own rows.

## Data model (migration 0004)
`customers`, `support_tickets`, `ticket_events`, `incidents`, `incident_events`, `deployments`, all tenant-scoped with composite FKs for event tables. No names, emails or free-text columns are exposed; analytics is aggregate-only. `service_name` is a plain string (no join in v1).

## Templates (`templates.py`)
`tickets_created`, `tickets_by_status`, `open_incidents_by_service`, `incidents_by_severity`, `incidents_by_root_cause`, `avg_first_response_by_service`, `deployments_per_day`. They only build intents; they go through the same validation.

## Production setup
1. Run migrations. 2. A DBA/Terraform job runs `psql -v ro_password=… -f infrastructure/sql/analytics_readonly_role.sql` (re-running rotates the password). 3. Set `DATABASE_READ_ONLY_ROLE_URL` to a DSN logging in as `ekc_analytics_ro`. Where only the main DSN exists, `AnalyticsExecutor(role_name="ekc_analytics_ro")` uses `SET LOCAL ROLE`.

## Tests (67)
Builder validation matrix (24 invalid intents), bound-value and single-SELECT checks, time range/bucket SQL, no sensitive columns exposed, strict parser, planner rejects unsafe model output (and its prompt has no secrets); real-DB: counts/groups/avg/sum/distinct, tenant isolation, truncation, SQL-injection inertness, **RLS without a tenant filter, RLS fail-closed, role login cannot INSERT/UPDATE/DELETE/DROP or read documents/users/embeddings, read-only transaction, statement timeout**. Full suite after fresh-DB alembic up/down/up: **448 passed**; ruff, format, `mypy --strict` clean.

A bug found by testing the shipped role script: psql does not substitute variables inside `DO $$` blocks; fixed with `format(%L)` + `\gexec`.

## Not verified / not built (honest)
- **`orders` and `products` tables from the spec are not built.** No joins (e.g. tickets ↔ customers) — a deliberate v1 limit.
- **The router → executor wiring doesn't exist yet**: nothing maps a `SQL_ANALYTICS` decision to a template/planner call until the orchestrator phase. `RAG_PLUS_SQL` fusion (§41) is NOT BUILT.
- The LLM planner has only been tested with a scripted fake model; real-model intent quality is NOT YET MEASURED.
- Production `ekc_analytics_ro` connection through a real managed Postgres (RDS etc.), pgbouncer behavior with `SET LOCAL`, and query performance on large tables are NOT YET MEASURED; no indexes beyond tenant_id exist for analytics queries.
- RLS covers SELECT only and does not bind the table owner by design; there is no RLS on the document tables yet.
- The tables have no ingestion path yet (no connector populates them); tests seed them directly.
