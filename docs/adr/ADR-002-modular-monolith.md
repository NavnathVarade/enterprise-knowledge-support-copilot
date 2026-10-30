# ADR-002: Modular Monolith for the Synchronous Path

## Context
The synchronous query path (query classification, hybrid retrieval, SQL analytics, LLM
generation, citations) and the asynchronous ingestion path (parse → chunk → embed → index)
have different scaling and failure characteristics. The brief (Section 145) forbids creating
microservices "for the sake of appearance."

## Decision
`apps/api` houses the full synchronous path as one deployable with clear internal module
boundaries (`packages/domain`, `rag/retrieval`, `rag/generation`, etc.), each importing only
the published interface of the others (no shared `packages/schemas` package was needed). Ingestion and the four
Kafka-consuming workers (parse/chunk/embed/index) are separate deployables
(`apps/ingestion-service`, `apps/worker-service`) because they have a genuinely different
scaling axis (Kafka partition count / consumer group size) and failure boundary (a stuck
embedding provider should not affect query latency).

## Alternatives considered
- **Full microservices per domain** (retrieval-service, generation-service, citation-service,
  ...): rejected for now — no evidence yet that these need to scale or fail independently of
  each other; the added network hops and deployment complexity have no offsetting benefit at
  current understood load.
- **Single monolith including workers**: rejected — synchronous request latency and
  asynchronous batch processing genuinely do have different scaling needs (Kafka consumer
  parallelism vs. HTTP request concurrency), and coupling their deployments would force
  over-provisioning one to scale the other.

## Consequences
- Internal module boundaries must be enforced by convention/lint rule (no domain importing
  another domain's internals directly), since there's no network boundary forcing it.
- If evaluation or production load later shows retrieval or generation needs independent
  scaling/failure isolation from the rest of `apps/api`, the extraction path is: promote the
  relevant `rag/*` package to its own `apps/*-service` with an HTTP or gRPC interface matching
  its current Python interface — the module boundary already exists, so this is a deployment
  change, not a redesign.

## Status
Accepted — Phase 1. **Amended — Phase 46:** the decision stands; two details changed. (1) The deployables are `apps/api`, plus `packages/workers` (ingestion consumer, outbox relay) running the same image as separate Deployments — the `apps/ingestion-service` / `apps/worker-service` directories planned here never held code and were deleted. (2) Module boundaries are *not* "by convention": `scripts/architecture_check.py` and `tests/unit/test_architecture_fitness.py` enforce a declared layer order and forbid import cycles between units (ADR-009).
