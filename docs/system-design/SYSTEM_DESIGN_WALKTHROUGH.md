# System design walkthrough (45-minute format)

Use this as a whiteboard script. Numbers marked *measured* come from this repository's stored results on a shared 2-core test machine and are **not** production predictions; everything else is a design target or reasoning.

## 1. Clarify (3 min)
Users: employees of many companies (tenants). Content: policies, runbooks, tickets, contracts. Hard requirements: a user must never get an answer built from a document they cannot access; answers cite sources; the system must abstain rather than guess. Soft: latency, cost control, auditability.

## 2. API and data (5 min)
Operations: login, upload document, list documents, ask a question, usage/budget, audit read/verify (`docs/reference/API.md`). Core tables: tenants, users, roles/groups, documents + versions + ACL rows, chunks, embeddings (1024-dim, HNSW), outbox, audit events (hash-chained), usage ledger.

## 3. High-level design (10 min)
```
Browser -> gateway/ingress -> web (static SPA) | API
API (sync): auth -> limits/budget -> router -> hybrid retrieval (ACL in SQL) -> context -> LLM gateway -> citation check -> audit
Upload: API -> object store + document row + outbox row (one transaction)
Worker path: outbox relay -> Kafka -> ingestion consumer: parse -> chunk -> embed -> index -> document active
State: Postgres (truth, vectors), Redis (throttles/limits only), Kafka (events), object store (originals)
```
Why this shape: the query path is latency-sensitive and CPU-light; ingestion is bursty and provider-bound, so they scale and fail separately (ADR-002). Details: `docs/architecture/PHASE_46_ARCHITECTURE_AS_BUILT.md`.

## 4. Deep dives (20 min) - pick by interviewer interest
1. **Authorization.** SQL `EXISTS` predicate on tenant and ACL rows inside each retrieval statement; no ACL row means no access (ADR-003). Re-checked when building context. Cost of building the predicate was measured and memoised (+28-38% throughput, *measured*).
2. **Hybrid retrieval.** Vector leg + lexical leg, each ACL-filtered, fused, then reranked. Filtered ANN pitfall: HNSW applies filters after the scan on older pgvector, silently returning few results (recall@50 0.05 for a small tenant at defaults on pgvector 0.6, *measured*); require pgvector >= 0.8.
3. **Ingestion reliability.** Transactional outbox, at-least-once delivery, idempotent consumers, retry + DLQ; relay is a single replica to preserve order, with a backlog-age alert (ADR-004).
4. **Cost and abuse control.** Per-user and per-tenant rate limits, daily token/cost budgets with an append-only usage ledger, login throttling.
5. **Failure behaviour.** Redis down: the login throttle and LLM rate limiter sit behind a circuit breaker and follow their configured fail-open/closed policy (login throttle default: allow, counted), readiness reports `degraded` (HTTP 200) rather than pulling every pod;  LLM down: retries, breaker, optional fallback model; DB pool exhausted: 503 with `Retry-After` (never 500).
6. **Audit and tamper evidence.** Append-only triggers + per-tenant hash chain with a verify endpoint; limits: a database superuser can still drop the triggers, chain head not externally anchored.

## 5. Scale and bottlenecks (5 min)
- One API worker saturated near 30 queries/s of *platform* work on the test machine; real LLM latency is excluded and dominates, so concurrency, not CPU, becomes the constraint. Scale API replicas horizontally (stateless).
- Vector index about 8 KB per chunk (819 MB at 100K chunks, *measured*): about 8 GB per million chunks (extrapolated), a single-node ceiling that eventually forces partitioning or a dedicated vector store.
- Ingestion throughput is bounded by the embedding provider (not measured); scale consumers up to partition count.

## 6. Operations and risks (2 min)
Thirteen alerts with runbooks, structured logs with request/trace ids, readiness gate (`docs/operations/PHASE_43_PRODUCTION_READINESS.md`). Honest risk list: provider latency unknown, no RLS backstop, no token revocation, in-process parsers, DR objectives unmeasured.

## 7. Follow-up questions to expect
Multi-region (not covered: documented gap), per-tenant encryption keys (not built), document deletion/retention (not built), evaluating answer quality (framework exists, no real data), moving to a dedicated vector DB (what would trigger it: filtered recall problems per-tenant indexes cannot fix, or index size beyond one node).
