# Interview guide

How to talk about this project honestly and in depth. Every story below is a real event in the repository history with its evidence; if you cannot point to the file, do not tell the story.

## 1. Thirty-second answer
"It's a multi-tenant RAG system where permission checks happen inside the retrieval SQL, so documents a user can't see never reach the model. Ingestion goes through a transactional outbox to Kafka. I built it in phases with one rule: no number without a command behind it, so I can tell you exactly what's verified and what isn't. It has never run against a real model or cluster, and my own readiness review says no-go for production."

## 2. Stories (situation, what I did, result, evidence)

**Measuring found what reading code did not: query plans (Phase 33).** Retrieval was fast in tests and 20–70× slower after five executions on a connection, because Postgres switched to a generic plan that could not see the tenant value (vector leg 26 ms → 583 ms at 10K chunks). Fix: `plan_cache_mode=force_custom_plan` per connection, with a regression test. Trade-off I can state: planning cost on every execution (about 1.5–5 ms). Evidence: `docs/performance/PHASE_33_PERFORMANCE.md` §2.1.

**The event loop was blocked by bcrypt (Phase 33).** Login did ~340 ms of CPU inline, stalling every other request on the worker (health checks p95 340 ms during logins). Moved hashing to a thread; same load p95 52 ms. I wrote a test that fails against the old code. Capacity consequence I can quote: about 3 logins/s per core, and no refresh tokens means every expiry is a login.

**A pool deadlock I created myself (Phase 45).** The query route held its DB connection while its audit write took a second one. At concurrency ≥ pool size, every request held one and waited for another: 16 concurrent queries gave 1.6 requests/s, 10 s median, 18 errors. Even one request on a one-connection pool silently lost its audit event, because that write is best-effort. Fix: release the first connection before auditing; now 31 requests/s, 0.45 s median, no errors. Lesson: nested resource acquisition, and "best effort" paths hide failures unless a test asserts the effect. Evidence: `docs/performance/PHASE_45_PERFORMANCE_AUDIT.md` §2.1.

**Profiling beat guessing (Phase 45).** A profile showed building the ACL SQL predicate was about a third of the handler's CPU. I memoised it, and because a wrong cache key there would be an authorization leak, I wrote tests that change each input one at a time and compare against an uncached build. Clean A/B: +28–38% throughput.

**The architecture drifted despite the ADR (Phase 46).** ADR-002 said boundaries would hold "by convention". An import-graph check found a five-package cycle and a second two-package cycle. I fixed them by moving value types down a layer, then replaced the convention with a test and CI step (ADR-009), and deleted seven empty scaffold packages the docs still described.

**Security audit found a live advisory (Phase 44).** `pip-audit` flagged `pypdf` (which parses untrusted uploads) and `certifi`. Bumped, pinned, regression-tested. Same phase: staging/production now refuse plaintext Postgres/Redis connection strings.

**End-to-end tests found two UI bugs and a test-isolation bug (Phase 42).** A form lost typed input when its initial fetch resolved late; sign-out left a return path so the next user landed on the previous user's page; and the browser tests' background consumer polluted other tests through the shared database.

## 3. Design questions and the honest answers

| Question | Answer |
|---|---|
| Why Postgres+pgvector, not a vector DB? | One system, ACL predicates are plain SQL, transactional with versions and the outbox (ADR-001). Cost: filtered-recall limits on pgvector < 0.8 (measured recall@50 of 0.05 for a small tenant at defaults on 0.6) and a single-node ceiling (~8 KB of index per chunk). |
| How do you stop one tenant seeing another's data? | Tenant and ACL predicates inside every retrieval statement, fail-closed when no ACL row exists, re-authorisation when building context, tests at SQL, HTTP and browser level. Gap: no row-level security backstop. |
| Why an outbox? | State change and event announcement must be atomic; a broker outage must not lose uploads. At-least-once, so consumers are idempotent; poison messages go to a DLQ (ADR-004). Never exercised against a real broker. |
| How do you defend against prompt injection? | Retrieved text is treated as data, screened and quarantined, escaped in the prompt, output policy and citation validation. Heuristic: say it cannot be guaranteed. |
| Why not text-to-SQL for analytics? | The model only emits a JSON intent over a fixed catalogue; templates run on a read-only role (ADR-007). Less flexible, much safer. |
| How do you know answers are good? | Honest answer: not known. The evaluation framework exists (Phase 27) but no real documents or real model were used; answer quality is not measured. |
| Why abstain so often? | Better to say "not enough evidence" than to answer from unauthorised or irrelevant text; the abstention rate has an alert. |
| What would break first at scale? | Postgres as the vector store and the single-replica outbox relay (availability); real LLM latency dominates everything. One API worker saturates near 30 queries/s of platform work on the 2-core test machine (not a production figure). |
| Security gaps? | No token revocation/refresh/MFA/key rotation, no RLS, in-process parsers, no document deletion, audit chain not externally anchored, no pentest (`docs/security/PHASE_44_SECURITY_AUDIT.md`). |

## 4. What I would do next (in order)
1. Run the CI pipeline and build images on a machine with registry access; fix what breaks.
2. Staging soak with a real LLM, real Kafka and S3; measure real latency, RPO/RTO.
3. Close security gaps: token revocation, sandboxed parsing, document deletion, RLS.
4. Evaluate answer quality on real documents with the Phase 27 framework.

## 5. Things not to say
That it is "production ready" (the review says no-go); any user, traffic or cost figure; that answer quality is good; that Kafka, S3 or the cloud infrastructure were exercised; that numbers from the 2-core sandbox predict production.
