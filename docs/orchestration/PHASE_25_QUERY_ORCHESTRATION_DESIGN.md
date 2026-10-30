# Phase 25 — Query Orchestration & `/api/v1/query` (Spec §6, §12)

First end-to-end request path. Everything from Phases 16–24 now runs behind one endpoint.

`POST /api/v1/query {"query": "..."}` → verified identity (tenant only from the token) → `route_query` → route pipeline → `QueryResponse {status, message, route, citations, analytics, payload, trace}`.

## Route behavior
| Route | Pipeline | Status values |
|---|---|---|
| RAG_KNOWLEDGE, HYBRID_SEARCH, LEXICAL_SEARCH, DOCUMENT_LOOKUP, UNKNOWN | ACL + temporal hybrid retrieval → rerank → context builder (re-authorize, quarantine injections, dedup, version pin, budget) → generation (versioned prompt, canary) → output policy → citation validation | `ANSWERED`, `ABSTAINED` |
| SQL_ANALYTICS | LLM → JSON intent → validated builder → read-only executor | `ANALYTICS_RESULT`, `NOT_SUPPORTED` |
| RAG_PLUS_SQL | RAG answer **and** analytics result, returned side by side | as above + `payload` |
| ACCESS_CHECK | ACL predicate on a document UUID found in the query; identical message for "no access" and "does not exist" | `ACCESS_RESULT`, `NEEDS_INPUT` |
| INCIDENT_ANALYSIS | `analyze_incidents` (role-gated) | `INCIDENT_REPORT`, 403 without role |
| SUPPORT_ANALYSIS | `investigate_customer_issue` for a customer UUID in the query (role-gated, ACL-enforced knowledge) | `INVESTIGATION_REPORT`, `NEEDS_INPUT` |
| DOCUMENT_COMPARISON | not implemented | `NOT_SUPPORTED` |

Historical questions ("in 2024") retrieve what was in effect at the **last moment** of the period (`as_of`, reported in the trace). To-date ranges and current/none use "now".

## Trace (observability, §27/§29)
`retrieved_candidates`, `context_chunks`, `context_tokens`, per-reason drop counts (unauthorized / not-in-effect / duplicate / injection / budget), `as_of`, `prompt_name/version`, `input_tokens`, `output_tokens`, `llm_model`, `llm_cost_usd` (null when no price is configured), `llm_attempts`, `llm_fallback_used`, `latency_ms`. One `query_executed` log line per request: tenant, user, route, status, query hash, trace scalars — **never the query text or answer**.

## Provider wiring (`packages/providers/factory.py`)
- **Staging/production refuse to start** without `EMBEDDING_API_KEY` and `LLM_API_KEY` (`ConfigurationError` in lifespan). The deterministic embedding provider is a test double with no semantic meaning and is only allowed in development/test.
- Reranker: Voyage when a key exists; otherwise the lexical-overlap reranker (functional, not semantic) with a loud log — degrade quality, not availability.
- LLM: no key ⇒ no generation. Never a fake answer.
- **Analytics runs only as the read-only role** (`DATABASE_READ_ONLY_ROLE_URL`). Without it, staging/production **disable analytics** instead of querying as the table owner (which would bypass RLS). Dev/test fall back to the main DSN with a warning.
- Keys are `SecretStr`: absent from `repr`/logs. Optional `LLM_INPUT_USD_PER_MILLION_TOKENS` / `LLM_OUTPUT_USD_PER_MILLION_TOKENS` enable cost accounting.
- Per-tenant LLM rate limit: `RATE_LIMIT_TENANT_QUERIES_PER_MINUTE` via Redis (fails open by default).

## Tests (33): 8 unit (factory) + 15 HTTP end-to-end + 1 fixed log test
Over real HTTP and Postgres with a scripted fake LLM: answer with citation/section/page and full trace; restricted and cross-tenant content never in the model prompt or response; empty corpus abstains with **zero** LLM calls; injected document quarantined; historical `as_of` handling; 401 without a token; real login→query path; request validation (422); access check with no existence oracle; NEEDS_INPUT/NOT_SUPPORTED routes call no model; incident analysis role-gated (403/200); analytics executes a planned intent for the caller's tenant only (tenant B's rows excluded). One pre-existing router test was made independent of global logging state (it failed only after the app had configured structlog). Full suite on a fresh DB after alembic up/down/up: **533 passed**; ruff, format, `mypy --strict` clean.

## Not verified / not built (honest)
- **No real LLM, embedding, or rerank provider was called.** Tests use a scripted model and the deterministic embedding; real answer quality, citation compliance, latency and cost are NOT YET MEASURED. The Anthropic/Voyage clients remain mock-tested only.
- **Document comparison** ("what changed between v3 and v4") is NOT BUILT; the router recognizes it but returns NOT_SUPPORTED.
- RAG+SQL is side-by-side, not jointly reasoned (§41 fusion NOT BUILT). Analytics intent planning is only exercised with a scripted model.
- No streaming, no conversation memory, no answer/response cache, no per-user rate limit (tenant-level only), no feedback endpoint, no audit-event publication to Kafka (log line only).
- Customer/document ids must be UUIDs typed in the query; there is no name resolution.
- Provider objects are built lazily on first request and cached on `app.state`; `validate_provider_configuration` runs at startup, but the first request still pays construction cost.
