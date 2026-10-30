# Authorization Design — Phase 4

## Scope delivered
- Password hashing (`packages/security/passwords.py`) — bcrypt, called directly (see bug #1 below)
- JWT issuance/verification (`packages/auth/jwt.py`) — RS256, with issuer/audience/expiry checks
- `Identity` (`packages/auth/identity.py`) — the single source of `tenant_id` for every
  authenticated request (Section 9)
- FastAPI dependencies (`apps/api/app/dependencies/auth.py`, `.../db.py`) —
  `get_current_identity`, `require_role(*roles)`, `get_tenant_session`
- `POST /api/v1/auth/login` and a minimal authenticated `GET /api/v1/documents`
  (`apps/api/app/routes/`)

## Design choices
- **Login requires `tenant_slug`, not email alone.** Two different tenants may have a user
  with the same email (Section 9); the client must know which tenant/workspace it's
  authenticating into, same as any real multi-tenant product.
- **The login error message is identical** whether the tenant slug, email, or password was
  wrong (`INVALID_CREDENTIALS` in all three cases) — distinguishing them would let an
  attacker enumerate valid tenant slugs or email addresses.
- **Roles/groups are embedded in the JWT**, not re-fetched from the database on every
  request, to avoid an extra DB round trip per request. This means a role/group change (or
  access revocation) only takes effect once the current token expires — bounded by
  `SECURITY_ACCESS_TOKEN_TTL_SECONDS` (15 minutes by default). Anything needing
  faster-than-token-TTL revocation is a documented gap for Phase 16 (a revocation-check
  path, e.g. a Redis denylist checked per-request), not something silently promised here.

## Proof, not assertion
`tests/integration/test_auth_and_tenant_isolation.py::
test_cross_tenant_document_isolation_over_real_http` is Phase 4's version of Section 127's
critical security E2E test: two tenants, each with a document, exercised through the real
request path — actual `POST /login` to get a real signed token, actual `GET /documents` with
that token as a bearer header — and asserts Tenant B's authenticated user sees exactly their
own document and none of Tenant A's. This runs against a real Postgres in CI on every push, not
a mock.

## Real defects found and fixed during this phase
1. **passlib is unmaintained and incompatible with bcrypt≥4.1.** The original implementation
   used `passlib[bcrypt]`, which failed a real test (`test_password_hash_roundtrip`) with
   `ValueError: password cannot be longer than 72 bytes` — for a password nowhere near that
   length. Root cause: passlib's bcrypt-backend version-detection shim breaks against bcrypt's
   post-4.0 API. Fixed by dropping passlib and calling `bcrypt` directly, with an explicit
   72-byte check that raises `PasswordTooLong` rather than silently truncating (silent
   bcrypt truncation is a known, historically-exploited footgun).
2. **`SET LOCAL x = :param` is a Postgres syntax error.** `packages/database/session.py`'s
   `tenant_scoped_session` (written in Phase 1, unexercised until this phase's real
   authenticated request path) tried to bind a parameter directly into a `SET LOCAL`
   statement, which Postgres doesn't support — `SET`/`SET LOCAL` only accept a literal or
   identifier there. Fixed using `set_config('app.current_tenant_id', :tenant_id, true)`,
   the correct parameterized equivalent, caught by
   `test_cross_tenant_document_isolation_over_real_http` failing with a `PostgresSyntaxError`
   the first time this function was actually exercised end-to-end.
3. A flaky test-timing bug (not a code bug): `test_expired_token_is_rejected` used a 1-second
   TTL with a 1.2-second sleep; `exp`/`iat` are `int()`-truncated Unix timestamps, which can
   lose up to ~1 second of precision depending on where "now" falls within its fractional
   second, making the margin a race. Fixed by widening the sleep to 2.5 seconds.

## Not yet implemented (explicitly, not silently)
- Token revocation / logout (a Redis denylist checked per-request) — Phase 16.
- Document-level ACL enforcement on the `/documents` route — it currently returns all of the
  tenant's documents, not filtered by `document_acl`; that's Phase 16's ACL-aware retrieval
  work. The route exists in Phase 4 purely to prove tenant isolation, not document-level
  authorization.
- Refresh tokens, password reset, account lockout after repeated failures, MFA — none in
  scope yet.
- `require_role` exists and is unit-testable but has no route using it yet (no admin-only
  routes exist yet to protect).
