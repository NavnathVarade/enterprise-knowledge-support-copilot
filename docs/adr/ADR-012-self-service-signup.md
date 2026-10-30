# ADR-012: Self-service organization signup, gated by environment

## Context
Until now tenants and users came only from `scripts/seed_database.py` (development) or operator tooling, so the web app
could not be tried by anyone without database access. A signup page was requested. The Phase 51 brief otherwise forbade
new endpoints, so this is a deliberate, minimal exception.

## Decision
One additive endpoint, `POST /api/v1/auth/signup`, which creates a **new tenant plus its first `admin` user** and returns
the same token shape as login. It never adds users to an existing tenant (that would let a stranger enter someone else's
workspace) and no existing route, schema or migration changed (tenants/users/roles tables already fit).

| Concern | Choice |
|---|---|
| Who may call it | `SIGNUP_ENABLED` unset = on in development/test, **off in staging/production**; production must opt in explicitly. Off → `403 SIGNUP_DISABLED` |
| Abuse | per-IP attempt counter in Redis (`SIGNUP_MAX_PER_IP`/`SIGNUP_WINDOW_SECONDS`, default 5/hour), every attempt counts, `429` + `Retry-After`; Redis outage follows `HARDENING_LOGIN_THROTTLE_FAIL_OPEN` |
| Credentials | policy: 10+ chars, ≤72 bytes (bcrypt), letter + digit, not containing the email local part; bcrypt hashing off the event loop; password never logged or audited |
| Integrity | unique slug enforced by the database; a lost race maps to `409 TENANT_SLUG_TAKEN`; one transaction for tenant, user, role, audit event |
| Audit | `auth.signup` in the new tenant's hash chain |
| Allowlist | added to the reviewed unauthenticated-route list in `tests/integration/test_route_authentication_audit.py` |

## Consequences
- The identifier being taken is observable (inherent to signup); the IP limit bounds probing. Login's no-enumeration
  property is unaffected.
- **Not built:** email verification, CAPTCHA, invitations, password reset. Without email verification, anyone can claim
  any email address inside their *own* new tenant; that grants nothing in other tenants. Production operators who enable
  signup should put a gateway rate limit and bot protection in front.
- The UI always offers the signup route; a deployment with signup off answers with an explanatory error.

## Status
Accepted — Phase 51.
