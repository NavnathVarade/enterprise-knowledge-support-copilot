# Phase 45 — Final performance audit

Builds on Phase 33. New this phase: the **query endpoint is now load-tested** (platform overhead only, see §1), which found one severe defect and one significant hotspot; the Phase 33 HTTP benchmark was re-run on the current code to look for regressions. Raw data: `docs/performance/results/query_phase45_*.json`, `api_1worker_phase45.json`. Tool: `scripts/bench_query.py`.

## 1. What is and is not measured

`POST /api/v1/query` was driven closed-loop against `tests/e2e/harness.py` (the real API, real Postgres + pgvector, real Redis; **stand-in** extractive LLM, deterministic embeddings, lexical reranker, loopback Kafka, in-memory object store) with 60 small synthetic documents ingested through the API. So the numbers are the platform's **own** per-question work: authentication, rate limiting, routing, ACL-aware hybrid retrieval, context building with re-authorisation and injection screening, citation validation, audit write, usage accounting, JSON. **Real LLM, embedding and reranker latency — normally the dominant cost — is excluded and NOT MEASURED**, as are Kafka, S3 and any real hardware.

Environment: the Phase 33 sandbox (2 shared cores, stock Postgres, 1 uvicorn worker that also runs the harness's relay/consumer loops; client, API, Postgres and Redis on the same cores). Dataset tiny (60 documents), so retrieval cost here is a floor, not a forecast. Single run per point; run-to-run noise not quantified.

## 2. Findings

### 2.1 Connection-pool deadlock on the query path (fixed) — severe
The query route kept the request session (and its connection) open while the best-effort audit write opened a **second** session. With N concurrent queries ≥ pool size, every request held one connection and waited for another until `pool_timeout`.

| Pool 5+2, closed loop, same code before/after | rps | p50 | errors |
|---|---|---|---|
| **Before**, 16 concurrent | **1.6** | **10,033 ms** | 18 × 503 (14 OK) |
| After, 16 concurrent | 31.3 | 453 ms | 0 |
| After, 32 concurrent | 31.0 | 716 ms (p99 3.5 s) | 0 |

Even a single request on a 1-connection pool lost its audit event after waiting the full timeout (the write is best-effort and swallows the failure, so this was **silent**). Fix: `_release_connection` returns the connection before the audit write (the query path never committed through that session, so rollback changes nothing else). Regression test: `test_query_and_its_audit_write_work_with_a_single_connection_pool` (fails on the old code with "took 3.1s", passes now). Phase 33's note about login holding a session while hashing is a related but different case and remains open.

### 2.2 ACL predicate construction was ~35% of the platform's per-query CPU (fixed)
A profile (pyinstrument, 150 sequential queries) showed `build_document_acl_predicate` — SQLAlchemy expression construction, called 4× per question — taking about a third of the time inside the query handler. It is now memoised (`lru_cache`, 2048 entries) keyed by **every** input the expression reads (tenant, user, department, groups, roles). Because a missed key field would be an authorization leak, `tests/unit/test_acl_predicate_cache.py` pins: cached output equals a fresh build, changing any single field changes the bound parameters, tenants never share an entry, and order of groups/roles is irrelevant.

Clean A/B (same pool 10+5, same harness, only the cache toggled):

| Concurrency | Before rps / p50 | After rps / p50 | Throughput |
|---|---|---|---|
| 1 | 15.1 / 63.2 ms | 19.4 / 49.5 ms | +28% |
| 4 | 20.2 / 192 ms | 27.2 / 145 ms | +35% |
| 8 | 19.8 / 385 ms | 27.4 / 288 ms | +38% |

### 2.3 Where the platform saturates
After both fixes one worker on this hardware saturates around **30 queries/s** (≈ 32 ms per question of the shared 2 cores, which also run Postgres, Redis and the load generator, so this is not the API process alone). Latency then grows linearly with concurrency (Little's law), with no errors. Capacity planning must add real provider latency per question (a query holds a worker task, not a CPU, while it waits on a provider, so provider latency affects concurrency more than throughput — but this was **not measured**).

### 2.4 Regression check against Phase 33 (1 worker, pool 5+2)
`results/api_1worker_phase45.json` vs `api_1worker_after_login_fix.json` (documents list, health, login):

| Scenario | Phase 33 | Phase 45 |
|---|---|---|
| health_live c=1 rps | 854 | 715 |
| health_ready c=1 rps | 342 | 258 |
| documents list c=8 rps / p50 | 147 / 49.9 ms | 253 / 30.4 ms |
| login c=1 p50 | 341 ms | 338 ms |
| health_live while 4 clients log in, p50 | 9.0 ms | 8.5 ms |

Authenticated reads got faster (the ACL cache) and login/head-of-line behaviour is unchanged. The two unauthenticated health paths are 16–25% slower at c=1. Plausible causes are the middleware added since Phase 33 (security headers, body limit) and the harness's background loops, but I did not isolate them, and noise was not quantified — treat it as an **unexplained difference, not a confirmed regression**. At 32 concurrent logins 4 requests got 503 (designed overload response).

## 3. Not measured / limitations
Real provider latency and cost; Kafka; S3; real hardware or Postgres tuning; more than 60 documents on the query path (Phase 33's 100K-chunk retrieval numbers still stand for the retrieval leg alone); multiple workers on the query path; concurrent ingestion + query; unexplained health-path difference above; resource requests/limits and HPA targets in Kubernetes remain untuned guesses; no soak test. SLO candidates from Phase 33 remain proposals.

## 4. Reproducing
Start the harness (`python -m tests.e2e.harness --port 8766 --state-file state.json`, test DB only, with the usual env plus very high `RATE_LIMIT_*` so the limiter does not distort the run) and run `python -m scripts.bench_query --base-url http://127.0.0.1:8766 --tenant SLUG --email E --password P --documents 60 --concurrency 1 8 16 32 --out out.json` using credentials from the state file. Stop the harness before running the test suite: its relay/consumer share the test database.
