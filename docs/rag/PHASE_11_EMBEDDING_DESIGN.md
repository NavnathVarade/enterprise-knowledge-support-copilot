# Embedding Pipeline & Vector Storage — Phase 11

(Covers the brief's Phase 11 "Embedding Pipeline" and the storage half of Phase 12
"Vector Storage", since the pgvector column and index already existed from Phase 3 and this
phase is what first actually exercises them end to end.)

## Scope delivered
- `rag/embeddings/base.py` — `EmbeddingProvider` Protocol (Section 23, 105)
- `rag/embeddings/voyage_provider.py` — a real HTTP client for Voyage AI's embeddings API,
  with batching, timeout, and bounded retry
- `rag/embeddings/deterministic_provider.py` — a reproducible local provider (see below)
- `rag/embeddings/batching.py` — pure batching helper
- `packages/database/repositories/embeddings.py` — persists vectors tied to `chunk_id` via the
  composite tenant-aware FK, plus `find_nearest_chunks` (a tenant-scoped pgvector cosine-distance
  query)

## The honest gap: no live embedding provider is reachable from this environment
Anthropic does not offer a native embeddings endpoint (the `provider="anthropic"` default in the
Phase 1 config was a placeholder pointing at Voyage, Anthropic's recommended embeddings partner).
`api.voyageai.com` isn't in this sandbox's network allowlist, and no API key is available here
regardless (confirmed: no credential environment variables; `api.anthropic.com` is reachable but
returns 401 without credentials). This is the same category of gap as the Kafka broker in Phase 8.

**What was done about it:**
- `VoyageEmbeddingProvider` is written against Voyage's documented request/response shape and
  tested with `httpx.MockTransport`, so its request construction, batching, retry, and error
  conversion are genuinely verified even though a live call is not.
- `DeterministicLocalEmbeddingProvider` maps text → a unit-length vector via a SHA-256-seeded
  PRNG. **It carries no semantic meaning** — "cat" and "kitten" are no closer than "cat" and
  "invoice". What it does honestly provide is a real, reproducible vector of the correct
  dimensionality, which lets the storage layer, the `<=>` cosine-distance query, the tenant
  scoping, and the dimension-mismatch rejection be tested against a real pgvector column.

**What is NOT verified**: embedding *quality* (does retrieval find semantically relevant chunks?),
live-provider latency/rate-limit behavior, and real cost. All `NOT YET MEASURED`. Retrieval-quality
claims can only be made in Phase 28 (evaluation) once a real provider is connected.

## Proof, not assertion
`tests/integration/test_embedding_pipeline.py`, against a real Postgres + pgvector:
- **Exact-match retrieval**: three chunks are embedded and stored; querying with one chunk's exact
  text returns that chunk as the nearest neighbor at ~zero cosine distance, with the other two
  strictly farther. A real assertion about the storage/query path, not the embedding quality.
- **Dimension mismatch (Section 23)**: inserting a 512-dim vector into the 1024-dim column is
  rejected by pgvector itself (`expected 1024 dimensions, not 512`).
- **Tenant isolation, twice**: the composite FK rejects an embedding claiming the wrong tenant for
  a chunk (extending the Phase 3 pattern to this table), and `find_nearest_chunks` returns only the
  querying tenant's chunks even when another tenant holds a byte-identical vector.

## Real defects found and fixed during this phase
1. **Retries silently never fired.** The first `VoyageEmbeddingProvider` caught
   `httpx.TransportError` and converted it to `EmbeddingProviderUnavailable` *inside* the
   `@retry`-decorated function. Tenacity's `retry_if_exception_type(httpx.TransportError)` only sees
   what the function raises *out*, so it only ever observed the already-converted domain error —
   never matching its condition, so it never retried. Found in review before the first test run;
   fixed by splitting into `_call_api_raw` (decorated, lets the raw exception through) and
   `_call_api` (converts after retries are exhausted). `test_transport_error_actually_retries_and_
   eventually_succeeds` is the permanent regression test — it asserts exactly 3 calls occurred
   (and takes ~8 real seconds because tenacity genuinely sleeps between attempts).
2. **A too-narrow test assertion** (not a system bug): the dimension-mismatch test expected
   `DataError`/`IntegrityError`, but SQLAlchemy surfaces asyncpg's error as its parent `DBAPIError`.
   The database was correctly rejecting the vector all along; the test now asserts the exact
   message instead, which is a stronger check.

## Not yet implemented (explicitly, not silently)
- **No embedding worker / Kafka consumer** — same "not wired to ingestion" gap as Phases 9-10.
- **No embedding cache** (Section 52) and **no re-embed-on-content-hash-change logic** (Section 19)
  — the `content_hash` exists on chunks but nothing yet skips re-embedding an unchanged chunk.
- **The 1024 dimension is fixed in the schema.** Switching to an embedding model with a different
  dimension requires a migration, not a config change (documented in `models/documents.py`).
- **No circuit breaker** (Section 142) around the provider — only bounded retry.
