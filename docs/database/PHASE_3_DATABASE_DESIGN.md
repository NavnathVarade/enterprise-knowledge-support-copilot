# Database Design — Phase 3

## Scope delivered
Identity domain (`tenants`, `users`, `roles`, `groups`, `user_roles`, `user_groups`) and
document domain (`documents`, `document_versions`, `document_acl`, `document_chunks`,
`embeddings`), as SQLAlchemy 2.0 models under `packages/database/models/`, with one Alembic
migration (`migrations/versions/9d3816ce05b0_...py`).

## Tenant isolation — enforced at the schema layer, not just in application code
Every child table references its parent via a **composite foreign key** on
`(tenant_id, parent_id)`, which requires the parent to carry `UNIQUE(tenant_id, id)`. This means
Postgres itself rejects an insert that pairs a real parent ID with the wrong tenant_id — a bug
class (e.g. a broken service accidentally attaching Tenant B's chunk to Tenant A's document)
that would otherwise only be caught by an application-level check, if at all.

**Proof, not assertion:** `tests/integration/test_schema_tenant_isolation.py::
test_composite_fk_rejects_cross_tenant_document_version` constructs exactly this scenario and
asserts Postgres raises `IntegrityError` referencing the `fk_document_versions_document`
constraint. This test is run against a real Postgres in CI on every push.

## CHECK constraints (Section 55)
Every enum-backed column (`status`, `visibility`, `security_classification`) has a matching
`CHECK` constraint mirroring the application-level `StrEnum`, so a direct SQL write that
bypasses the ORM (an admin script, a bug in a future service) cannot leave the column holding a
value the rest of the system doesn't understand. `document_acl` additionally has a
`grant_matches_visibility` CHECK ensuring a row claiming e.g. `visibility='user_specific'`
actually names a user — an inconsistent ACL row here would be a silent authorization bug.
Proven by `test_check_constraint_rejects_invalid_document_status` and
`test_check_constraint_rejects_user_specific_acl_without_user`.

## Vector storage
`embeddings.embedding` is a `pgvector` `VECTOR(1024)` column with an HNSW index
(`vector_cosine_ops`), matching the cosine-similarity retrieval strategy from Phase 0 Section 15.
The dimension is currently hardcoded to match `EmbeddingSettings.dimensions`; changing embedding
models to a different dimension requires an explicit migration (new column + backfill), not a
silent schema change — see the note in `packages/database/models/documents.py`.

## Real defects found and fixed during this phase
1. **Alembic naming-convention double-expansion.** With a custom `naming_convention` on
   `Base.metadata`, both `op.create_check_constraint` and `op.drop_constraint` expand their
   `name` argument through the same convention. Passing the already-expanded name to
   `drop_constraint` (as the first hand-edit attempted) double-prefixes it and targets a
   constraint that was never created. Root-caused by running a genuine
   upgrade → downgrade → upgrade cycle against a live database (not just `upgrade`), which is
   why that three-step cycle is now a required CI step, not just a local sanity check.
2. **pytest-asyncio per-test event loops vs. a cached AsyncEngine.** `packages/database/
   session.py` deliberately caches one `AsyncEngine` per process (correct for a production
   service running on one event loop for its whole lifetime). pytest-asyncio's default of a new
   event loop per test function broke that assumption. Fixed via
   `asyncio_default_fixture_loop_scope`/`asyncio_default_test_loop_scope = "session"` in
   `pyproject.toml`, which matches the tests' execution model to the real one instead of
   special-casing the code under test.

## Not yet implemented (explicitly, not silently)
- Postgres Row-Level Security (RLS) policies — the composite-FK pattern above is real
  defense-in-depth, but RLS would be an additional, stronger layer; deferred to a phase where
  the query/repository layer (which RLS's `current_setting('app.current_tenant_id')` needs to be
  set correctly by) exists.
- `support_tickets`, `incidents`, `prompts`, `evaluation_*`, `audit_events`, and the
  query/retrieval-result tables from Section 54 — scoped to later phases as they become relevant
  (support/incident intelligence in Phases 25-26, evaluation in Phase 28, LLM gateway/audit in
  Phases 19-20).
- Repository/query layer enforcing `tenant_id` on every read — the schema-level protection above
  covers writes; read-path tenant filtering is Phase 4/16 work.
