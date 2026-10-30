# Synthetic Enterprise Dataset — Phase 6

## Scope delivered
- `data/synthetic/models.py` — pure dataclasses for the generated dataset
- `data/synthetic/generator.py` — deterministic generator (`generate_dataset(seed=1337)`),
  no database or I/O dependency
- `scripts/seed_database.py` — idempotent script mapping the generated dataset onto the real
  ORM models via the Phase 3-5 repository layer

## Dataset contents
Two tenants (Acme Corp, Globex Corp — deliberately different numbers in their conflicting
policies so the tenants aren't literally identical datasets), each with:
- The six named personas from Section 175 (Alice/Engineering, Bob/HR, John/Finance,
  Carol/Support, Admin, Security Auditor), each with a role and department
- Ten documents covering the scenarios Section 174 explicitly calls for:

| Scenario tag | What it tests |
|---|---|
| `versioning` | A document with a superseded v1 and current v2 (feeds Phase 5's temporal tests) |
| `conflicting_policy_a` / `_b` | Two differently-titled documents claiming different numbers for the same real-world question (Section 31) |
| `restricted_role` | Document visible only to the `security_auditor` role |
| `restricted_user` | Document visible only to one specific user |
| `public_engineering` | An ordinary tenant-visible document |
| `stale_archived` | A document whose lifecycle status is `archived` |
| `duplicate_a` / `duplicate_b` | Two differently-titled documents with byte-identical content (for future duplicate-detection work) |
| `prompt_injection` | A document containing an embedded instruction-override attempt — a defensive-security test fixture for Phase 27's prompt-injection-defense work, not exploit content |

## Determinism — proven, not assumed
`generate_dataset()` uses no wall-clock time and no unseeded randomness anywhere; all
timestamps are computed relative to a fixed reference instant. `test_generation_is_deterministic`
and `test_generation_has_no_wall_clock_dependency` call the generator twice and assert
dataclass-equality on the results.

## Idempotency — proven against a real database
`test_seeding_twice_is_idempotent` runs the full seed script twice against a real Postgres and
asserts tenant/user/document counts are identical after both runs — not inferred from the
"get or create" pattern used throughout the script, but actually exercised.

## Not yet implemented (explicitly, not silently)
- No malformed/invalid records in the dataset yet (Section 174 also asks for these) — they're
  deferred to Phase 7, since a malformed record only makes sense as a *rejected* input to the
  ingestion validator, which doesn't exist yet.
- No incidents/support-tickets/customers data yet — that's Phase 25-26 (support/incident
  intelligence) territory; today's dataset is document-only.
- The prompt-injection document exists as data but nothing yet reads it through an LLM — its
  actual defensive value is proven in Phase 27, not this phase.
