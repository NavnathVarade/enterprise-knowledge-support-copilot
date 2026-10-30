# Phase 33 — Performance and load testing (Spec §119, §120)

Every number below was produced by the scripts in `scripts/bench_*.py` and is stored as JSON in
`docs/performance/results/`. Nothing here is a production prediction: see §6 for what was not
measured, and §1 for the hardware.

## 1. Environment (all runs)

| | |
|---|---|
| Hardware | 2 logical cores (Intel Xeon @ 2.8 GHz), 8 GB RAM, shared by the load generator, the API, Postgres and Redis |
| Software | Python 3.13, PostgreSQL 16.15, pgvector **0.6.0** and **0.8.0** (both tested, stated per table) |
| Postgres config | stock Ubuntu defaults (`shared_buffers` 128 MB) — not tuned |
| API | uvicorn, 1 or 2 workers, `ENVIRONMENT=test`, DB pool 5 + 2 overflow (from `.env.test`; the product default is 10 + 5) |
| Data | **synthetic**: 1024-dim Gaussian-mixture vectors (not real embeddings), Zipf text, 10 tenants, 10 chunks/doc, 20% of documents restricted to another department |
| Method | single client, sequential queries (retrieval); closed-loop, 10 s per point (HTTP). 100 queries per retrieval point after 5 warm-ups |

Because the vectors are synthetic and clustered, **recall and latency on real embeddings and real text will differ.**

## 2. Findings that changed the product

Three defects were found by measuring, not by reading code. Each was fixed and has a regression test.

### 2.1 Generic query plans made retrieval 20–70× slower (fixed)

SQLAlchemy's asyncpg driver prepares statements. After 5 executions on a connection Postgres may
switch to a *generic* plan that cannot see the `tenant_id` value. Measured on 1,000 chunks per
tenant (10K total), identical queries, only the execution count differed:

| Query | first executions | after the switch |
|---|---|---|
| ACL-filtered vector leg, p50 | 26 ms | 583 ms |
| ACL-filtered lexical leg, p50 | 12 ms | 830 ms |

Fix: `packages/database/session.py` sets `plan_cache_mode=force_custom_plan` on every connection.
After the fix the same queries stayed at 22–29 ms and 8.7 ms. Test:
`tests/integration/test_db_plan_cache_mode.py`. Trade-off: planning happens on every execution
(about 1.5–5 ms for these queries).

### 2.2 bcrypt ran on the event loop and stalled every request (fixed)

Login did ~340 ms of CPU inline. One worker could therefore serve ~3 logins/s *and* nothing else
while they ran. With 4 clients logging in, health-check requests on the same worker:

| | requests/s | p50 | p95 | p99 |
|---|---|---|---|---|
| bcrypt inline (before) | 32.6 | 47.9 ms | 340 ms | 568 ms |
| bcrypt in a worker thread (after) | 297.8 | 9.0 ms | 52 ms | 117.5 ms |
| same, 2 workers | 439.3 | 6.4 ms | 18 ms | 102 ms |

(Idle health check: p50 ≈ 1.1 ms, ~840 requests/s on one worker.) Fix: verification and the
timing-equalisation dummy check run through `asyncio.to_thread`. Test:
`test_login_does_not_block_the_event_loop` (a blocking 0.4 s verify must not stall a ticker; I
confirmed the test fails with the old inline code).

**Capacity implication:** login throughput is CPU-bound at roughly 6 logins/s on these 2 cores
(≈3 per core). There are no refresh tokens, so every token expiry is a full login; size login
capacity from token lifetime and active users, or lower the bcrypt cost only as a deliberate
security decision.

### 2.3 Pool exhaustion returned HTTP 500 (fixed)

At 32 concurrent logins with a 5+2 pool, 1–2 requests per run failed with a 500 because a
request holding a DB connection waited longer than `pool_timeout`. Overload is now a retryable
**503 `SERVICE_BUSY` with `Retry-After: 1`**. Test: `test_db_pool_exhaustion_is_503_with_retry_after_not_500`.
**Not fixed:** the login route still holds its DB session while hashing, so login can tie up
connections; a separate short-lived session around the lookup would be the structural fix.

### 2.4 HNSW returns far too few results for small tenants on pgvector < 0.8 (mitigation added, not a default)

The vector index is global; the tenant and ACL filters are applied after the index scan on
pgvector 0.6. Query tenant = 10% of the table, `limit` = 50 (the hybrid candidate count),
100K chunks, pgvector **0.6.0**:

| `hnsw.ef_search` | p50 | p95 | recall@50 vs exact (mean / min) | results returned (of 50) |
|---|---|---|---|---|
| 40 (default) | 11.3 ms | 14.9 ms | 0.054 / 0.0 | 3.2 |
| 100 | 15.6 ms | 21.6 ms | 0.133 / 0.0 | 8.0 |
| 200 | 19.2 ms | 37.5 ms | 0.278 / 0.0 | 15.6 |
| 400 | 23.1 ms | 29.7 ms | 0.602 / 0.0 | 31.5 |

It is fast because it is wrong: the vector leg silently contributes almost nothing and hybrid
search degrades toward lexical-only, with no error. At 10K chunks the planner chose an exact
per-tenant scan and recall was 1.0 at every setting, so **small test datasets hide this**.

Same data, pgvector **0.8.0** (built and installed in the sandbox), default settings:

| `hnsw.ef_search` | p50 | p95 | p99 | recall@50 | returned |
|---|---|---|---|---|---|
| 40 / 100 / 200 / 400 | 53.7–57.6 ms | 64–70 ms | 71–74 ms | 1.0 | 50 |

`hnsw.iterative_scan=relaxed_order` gave the same recall and latency here (56–59 ms p50). On this
dataset 0.8's planner avoided the failing plan, so this run does **not** show the iterative scan
rescuing a case that 0.8 would otherwise get wrong; that case (a tenant that is a much smaller
share of a much larger table, or a restrictive ACL on a big tenant) is **NOT YET MEASURED**.

What changed in the product:
- New `RETRIEVAL_HNSW_EF_SEARCH`, `RETRIEVAL_HNSW_ITERATIVE_SCAN`, `RETRIEVAL_HNSW_MAX_SCAN_TUPLES`
  (all unset = database defaults), applied transaction-locally so they never leak across pooled
  connections. Tests: `tests/integration/test_hnsw_query_settings.py`.
- Compose and CI now pin `pgvector/pgvector:0.8.0-pg16` (previously the floating `pg16` tag).
- **Operator rule:** run pgvector ≥ 0.8. Setting `iterative_scan` on 0.6/0.7 makes the query fail
  because the parameter does not exist there. Whether to set it is a measurement to repeat on
  your real data; this phase only provides the switch.
- Not built: a shortfall detector or exact-scan fallback, per-tenant partitioned indexes. These are
  the next options if real data shows the problem on 0.8.

## 3. Retrieval latency and size (pgvector 0.8.0 for 100K; 0.6.0 for 10K)

ACL- and temporally-filtered queries exactly as the product runs them, `limit`=50 per leg,
fused to 10, query embedding pre-computed (provider latency excluded). Querying tenant holds 10%
of chunks.

| Chunks | Vector leg p50 / p95 | Lexical leg p50 / p95 | Hybrid (`hybrid_search_authorized`) p50 / p95 / p99 | HNSW index | Chunks + embeddings tables |
|---|---|---|---|---|---|
| 10,000 (0.6.0) | 25.6 / 34.7 ms | 10.2 / 14.6 ms | 42.6 / 50.3 / 58.5 ms | 82 MB | 160 MB |
| 100,000 (0.8.0) | 53.9 / 64.5 ms | 47.2 / 67.6 ms | 107.6 / 129.2 / 145.7 ms | 819 MB | 1,567 MB |

Observations: latency grows roughly with the *tenant's* size, not the table's (the tenant filter
dominates the plan); the lexical leg at 100K (~47 ms) now rivals the vector leg because the
OR-tsquery ranks every matching row. The 100K index needs ~0.8 GB; at 1M chunks expect ~8 GB for
the index alone, which will not fit the sandbox's RAM (so 1M was **not** attempted).

Ingestion write rate (bulk insert of chunk + 1024-dim embedding rows, one connection, HNSW index
maintained): 10K rows in 50 s (~200 rows/s); 100K rows in 690 s (~145 rows/s), slowing as the
index grows. Chunking itself is not a bottleneck: ≥7.6 M words/s on one core
(`results/chunking.json`). With real embedding calls, provider latency will dominate and was not measured.

## 4. HTTP API (one client process, same machine)

`GET /api/v1/health/*`, `GET /api/v1/documents` (authenticated, 2 seeded tenants, few rows) and
`POST /api/v1/auth/login`. Closed-loop 10 s per point; closed loops understate latency under
overload (coordinated omission), so read the curve, not a point. Full tables in
`results/api_*.json`.

| Scenario (1 worker, after fixes) | c=1 rps / p50 | c=8 rps / p50 / p99 | c=32 rps / p50 / p99 |
|---|---|---|---|
| health_live | 854 / 1.1 ms | 963 / 5.3 / 95.8 ms | 639 / 34.7 / 221 ms |
| health_ready (DB + Redis) | 342 / 2.8 ms | 456 / 15.4 / 45.9 ms | 318 / 64.6 / 458 ms |
| documents list (authenticated) | 110 / 8.6 ms | 147 / 49.9 / 166 ms | 153 / 191.8 / 754 ms |
| login | 2.9 / 341 ms | 5.9 / 1,320 / 1,659 ms | 6.0 / 4,448 / 10,249 ms |

With 2 workers the documents list reached ~200–211 rps (c=8/32, p50 41.5 / 95.5 ms), roughly
+30–45%, not +100%: the client, Postgres and Redis share the same 2 cores, so this is a lower
bound for scaling out, not a measure of it.

## 5. SLO candidates (not claims)

These are proposals to validate on production-like hardware, derived from where the measurements
above sit. None is asserted as met.

| Objective | Candidate | Evidence today |
|---|---|---|
| Retrieval (hybrid, DB only), 100K chunks, p95 | ≤ 250 ms | 129 ms on shared 2-core sandbox |
| Retrieval completeness | ≥ 99% of requests return `limit` results when ≥ `limit` authorised chunks exist | only measurable with a shortfall metric (not built) |
| Login, p95 | ≤ 1 s at expected login rate | 341 ms unloaded; saturates near 6/s here |
| Non-login API (cheap reads), p99 | ≤ 500 ms at design concurrency | 754 ms at c=32 on one worker, saturated |
| Overload behaviour | 503 + Retry-After, never 500 | implemented, unit-tested |
| End-to-end answer, ingestion throughput, Kafka throughput | none proposed | **NOT YET MEASURED** |

## 6. Not measured / limitations

- **LLM, embedding and rerank provider latency and cost.** No live provider here. (`/api/v1/query` platform overhead was load-tested in Phase 45 with stand-in providers: `PHASE_45_PERFORMANCE_AUDIT.md`.)
- **Kafka**: no broker, so producer/consumer throughput, lag behaviour and the outbox relay under load are NOT YET MEASURED.
- **Scale**: 1M documents/chunks and 10K/100K users were not run (RAM, time). 100K chunks is the largest dataset.
- **One large tenant / one tiny tenant at scale**, and ACL selectivity other than 20% restricted, were not varied (see 2.4).
- **Concurrent retrieval load**: retrieval was measured with one sequential client; concurrency behaviour of Postgres under many simultaneous vector searches is NOT YET MEASURED.
- **Hardware**: shared 2 cores, untuned Postgres, no network hop between services. Production numbers will differ in both directions.
- **Real data**: synthetic vectors/text only; recall on real embeddings is unknown.
- **No resource sizing validated**: HPA/requests/limits in the Kubernetes manifests remain untuned guesses.
- Run-to-run noise was not quantified beyond the percentiles shown (single run per point).

## 7. Reproducing

```
# retrieval (database name must end in _bench; the script refuses otherwise)
DATABASE_URL=postgresql+asyncpg://.../ekc_bench PYTHONPATH=. \
  python -m scripts.bench_retrieval --chunks 100000 --tenants 10 --out result.json
python -m scripts.bench_retrieval --reuse --session-setting hnsw.iterative_scan=relaxed_order --out r2.json
# HTTP (against a running API seeded with the synthetic dataset)
PYTHONPATH=. python -m scripts.bench_api --base-url http://127.0.0.1:8765 --tenant acme-corp \
  --email alice@example.com --password alice-synthetic-pw-1 --out api.json
PYTHONPATH=. python -m scripts.bench_chunking --out chunking.json
```

The 100K load takes about 12 minutes on the sandbox.

## 8. Tests added

`test_db_plan_cache_mode.py` (1), `test_hnsw_query_settings.py` (4, one skips on pgvector < 0.8),
`test_login_does_not_block_the_event_loop`, `test_db_pool_exhaustion_is_503_with_retry_after_not_500`.

Full suite: **779 passed**; ruff and mypy strict clean; bandit clean; migrations upgrade →
downgrade → upgrade on a fresh database (pgvector 0.8.0 in the sandbox; earlier phases ran on 0.6.0).
