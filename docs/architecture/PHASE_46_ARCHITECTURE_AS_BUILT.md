# Phase 46 — Architecture audit and as-built architecture

This supersedes the *service boundary* part of `PHASE_0_ARCHITECTURE.md`; the rest of that document (data model, security model, failure behaviour) remains the design intent and is cross-checked below.

## 1. Audit method
1. Built the runtime import graph of `packages`, `rag`, `apps` (`scripts/architecture_check.py`) and looked for cycles and upward dependencies.
2. Compared the Phase 0 design and ADR-002 against the tree.
3. Checked that every decision that matters has a recorded rationale.

## 2. Findings and fixes

| # | Finding | Fix |
|---|---|---|
| 1 | **Five units in one import cycle**: `packages.domain` → `packages.database` (for `DocumentStatus`), `packages.database` → `rag.chunking` (for `Chunk`), plus `auth`/`parsing` | `DocumentStatus` and `Chunk` moved into `packages.domain` (old import paths still work: `rag.chunking.base` re-exports `Chunk`) |
| 2 | `rag.citations` ↔ `rag.generation` cycle (`SourceRef`) | `SourceRef` moved to `rag.citations.source_ref`; `prompt_builder` re-exports it |
| 3 | ADR-002 promised convention-based boundaries; the convention had failed (1, 2) | Boundaries are now enforced by a test (ADR-009) and checked in CI |
| 4 | Seven empty scaffold packages from Phase 0 (`apps/{admin,evaluation,ingestion,worker}-service`, `packages/{embeddings,retrieval,schemas}`) that documents described as real | Deleted; Phase 0 §4 and ADR-002 carry an amendment note |
| 5 | ADRs 001, 003, 004, 007 were listed in Phase 0 but never written | Written, with measured consequences and open gaps (ADR-009 is new) |

Result: no cycles, no layer violations; two reviewed lazy imports remain (`packages.config` → `packages.security` for the committed-test-key check, which breaks a config↔security cycle; `apps.api` → `rag.ingestion` in one route).

Not written: Phase 0's ADR-005/006/008/010 titles (outbox is covered by ADR-004; RRF fusion, prompt versioning and the temporal model are documented in their phase documents but have no ADR).

## 3. As-built structure

Deployables (one image, three Deployments): **API** (`apps/api`, serves `/api/v1/*`), **ingestion worker** and **outbox relay** (`packages/workers`), plus the **web** SPA image (`apps/web`) behind the gateway/ingress.

Layers (a unit may import only its own layer or lower; enforced):

| Layer | Units |
|---|---|
| 0 leaves | `packages.config`, `domain`, `events`, `observability`, `redis`, `security` |
| 1 adapters/persistence | `packages.database`, `auth`, `storage`, `kafka`, `audit`, `usage`, `llm` |
| 2 pure RAG blocks | `rag.parsing`, `chunking`, `embeddings`, `reranking`, `security`, `routing`, `ingestion` |
| 3–5 RAG stages | `rag.retrieval`, `context`, `citations` (3), `generation` (4), `comparison` (5) |
| 4–6 services | `packages.analytics`, `investigation` (4), `ingestion` (5), `orchestration`, `providers` (6) |
| 7–8 top | `rag.evaluation` (7), `packages.workers`, `apps.api` (8) |

Query path: gateway → API middleware (body limit, request context, security headers, metrics) → JWT identity → rate limit/budget → router → ACL-filtered hybrid retrieval → re-authorised context with injection screening → LLM gateway (retries, breaker, fallback) → citation/output validation → audit write → response. Ingestion path: upload → object store + `documents`/outbox row (one transaction) → relay → Kafka → consumer parses, chunks, embeds, indexes → document `active`.

State: Postgres (system of record, pgvector, audit chain, usage ledger, outbox), Redis (throttling, rate limits, breakers only; never a source of truth), Kafka (events), object store (originals).

## 4. Design intent vs reality (Phase 0 cross-check)
| Phase 0 claim | Reality |
|---|---|
| Five services | API + workers + web (see §3) |
| Redis for cache | Not used as a result cache; only throttle/limits/breakers |
| Postgres RLS "documented as a defence-in-depth addition" | Still not implemented (ADR-003) |
| No localhost between services in deployed config | Enforced by settings validator; TLS to Postgres/Redis also enforced (Phase 44) |
| Documentation produced incrementally | True per phase, except the ADR gaps closed here |

## 4b. Environment modes (Phase 50)

`Settings` has two explicit modes (ADR-011). Development runs with Kafka off: `packages/workers/local_dispatch.py` drains the same Postgres outbox in-process and calls the same ingestion handler, and `LocalFileObjectStore` replaces S3. Staging/production (the default when `ENVIRONMENT` is unset) require Kafka, TLS and explicit endpoints and reject both substitutes. `apps.api` lazily imports `packages.workers` only when the dispatcher is enabled (recorded in the architecture fitness test).

## 5. Residual architectural risks
Single-replica outbox relay (availability, not durability); Postgres is the scaling bottleneck for vectors (ADR-001); no RLS backstop; in-process parsers; ACL predicate correctness depends on every query using it; unit-granular boundary checks do not police imports inside a unit.
