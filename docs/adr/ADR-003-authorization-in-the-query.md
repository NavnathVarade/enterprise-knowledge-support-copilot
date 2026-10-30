# ADR-003: Authorize in SQL before retrieval, fail closed

## Context
"Authorization must happen before context reaches the LLM." Post-filtering fetched rows leaks through timing, ranking and bugs.

## Decision
Every retrieval statement ANDs `tenant_id` and an `EXISTS` ACL predicate (`build_document_acl_predicate`) into its WHERE clause. A document with no ACL rows is visible to nobody. Context construction re-checks authorization on the chunks it is handed. The predicate is memoised on its complete input set (Phase 45).

## Alternatives considered
- Filter in Python after retrieval: simpler, but unauthorised text would already be in application memory and could reach a reranker or prompt through a bug.
- Postgres row-level security as the primary mechanism: attractive defence in depth, **not implemented** (documented gap).

## Consequences
- Strong, testable property (tenant-isolation, ACL and E2E tests).
- Correctness depends on every new query using the predicate; there is no database-level backstop until RLS exists.
- The predicate cache key must include every input it reads (test pins this).

## Status
Accepted — Phase 16.
