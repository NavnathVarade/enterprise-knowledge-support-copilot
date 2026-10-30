# ADR-001: pgvector inside PostgreSQL rather than a dedicated vector database

## Context
Retrieval must apply tenant and document ACL filters *inside* the query (Phase 16), combine vector and lexical search, and stay transactionally consistent with document versions and the outbox.

## Decision
Store embeddings in Postgres (pgvector, HNSW) next to chunks, ACL rows and versions. Hybrid search is two SQL legs fused in application code.

## Alternatives considered
- Dedicated vector DB (Pinecone, Qdrant, OpenSearch): better scale-out and filtered-ANN features, but ACL filtering would have to be mirrored into a second system and kept consistent; rejected for now.
- Postgres full-text only: no semantic recall.

## Consequences
- One system to back up, secure and keep consistent; ACL and tenant predicates are ordinary SQL.
- **Measured limits (Phase 33):** the global HNSW index applies filters after the scan, so on pgvector < 0.8 a small tenant can silently receive far fewer than `limit` results (recall@50 of 0.05 at default `ef_search`). Run pgvector >= 0.8; iterative scan is a switch, not a verified fix for very small tenants in very large tables. A shortfall detector is not built.
- Index size (~8 KB/chunk) bounds a single node: 1M chunks needs ~8 GB for the index alone (extrapolated, not measured).
- Revisit if real data shows filtered recall problems that per-tenant partial indexes cannot fix.

## Status
Accepted — Phase 3; consequences updated Phase 46.
