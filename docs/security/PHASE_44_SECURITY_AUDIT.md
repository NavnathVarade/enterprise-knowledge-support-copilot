# Phase 44 — Final Security Audit

Scope: the code and configuration in this repository, audited on 2026-10-25. This is a self-audit with automated tooling and targeted tests. It is **not** an independent penetration test, and nothing here was exercised against a deployed environment.

## 1. Method and evidence

| Method | Tool / test | Result |
|---|---|---|
| Known-vulnerable dependencies (production pins) | `pip-audit` on the production pins (then `requirements/constraints.txt`; now `uv export --frozen --no-dev` from `uv.lock`) | **4 advisories found** (pypdf 6.21.1 ×3, certifi 2026.7.22); fixed by bumping to pypdf 6.21.2 / certifi 2026.10.19; re-scan: none |
| Static analysis | bandit (config in `pyproject.toml`) | No unsuppressed findings (suppressions are line-local `nosec` comments) |
| Secret scanning | gitleaks on the working tree | 0 findings |
| Frontend dependencies | `npm audit --omit=dev` (CI, high+ blocks) | 2 moderate react-router advisories remain, documented in Phase 41 (need a v7 migration) |
| Authentication coverage | `tests/integration/test_route_authentication_audit.py` enumerates every route (OpenAPI + router table) and requires 401 for no/garbage token, except a 3-entry reviewed allowlist (login, live, ready) | PASS; a new unauthenticated endpoint now fails CI |
| Dangerous-pattern review | grep for `verify=False`, `yaml.load`, `pickle`, `eval/exec`, `shell=True`, wildcard CORS, SQL built from strings | Only identifier/int interpolations remain, each validated (`SET LOCAL ROLE` is guarded by an identifier regex; timeouts are validated ints); `shell=True` only in the chaos script (dev tool, `nosec`) |
| Transport security of dependencies | new settings rule + tests | see below |
| Prior phases' evidence (unchanged) | tenant isolation, ACL-aware retrieval, prompt-injection quarantine, output policy, audit hash chain with tamper detection, CSP, rate limits | covered by their own suites; all green in this run |

## 2. Defects found and fixed

1. **Vulnerable PDF parser and CA bundle.** `pypdf` parses untrusted uploads, so its advisories are directly reachable. Pinned fixed versions (now in `uv.lock`), raised the floor in `pyproject.toml`, and added a regression test (`test_dependency_floor.py`). The CI `security-scan` job already fails on advisories, so this would have gone red.
2. **Plaintext Postgres/Redis connections were accepted in production** (Phase 43 blocker 7). Staging/production now refuse to start unless every Postgres DSN (including the analytics read-only role) has `ssl=require|verify-ca|verify-full` and Redis uses `rediss://`. The only bypass is the explicit `HARDENING_REQUIRE_DEPENDENCY_TLS=false`, intended for a trusted private network. The Kubernetes ConfigMap and Helm values now use `rediss://`; the Compose file runs as `ENVIRONMENT=development` and is unaffected.

   Caveat: this validates the *connection string*, not the server. `ssl=require` encrypts but does not verify the certificate; prefer `verify-full` with the CA mounted when the CA is available. Whether Redis/RDS actually have TLS enabled is infrastructure (Terraform enables it; never applied).

## 3. Threat-model walk-through (STRIDE, abbreviated)

| Threat | Control in place | Residual risk |
|---|---|---|
| Spoofing: stolen/forged token | RS256, issuer/audience/expiry checks, tenant bound in claims, committed test key refused outside dev/test | No revocation, refresh tokens, MFA or key rotation: a leaked token works until expiry |
| Tampering: audit log | Append-only trigger + hash chain with verify endpoint | A DB superuser can still disable the trigger; ship the chain head to external storage to close this |
| Repudiation | Audit events for logins, queries, admin actions | Chain not anchored externally |
| Information disclosure: cross-tenant / ACL | Tenant-scoped queries, ACL filtering inside retrieval SQL, abstention when nothing is permitted, E2E test | Depends on every new query being tenant-scoped; covered by tests, not by a DB-level policy (no row-level security) |
| Information disclosure: LLM leakage | Retrieved text is data, injection quarantine, output policy, citation enforcement | Heuristic defences; no guarantee against novel injections |
| Denial of service | Body-size cap, rate limits, login throttle, budgets, circuit breakers, timeouts | Parsers run in-process: a malicious file can still burn CPU/memory of a worker |
| Elevation of privilege | Role checks on admin/audit routes, non-root read-only containers, PSA restricted, default-deny network policy | Parser compromise would run with the app's privileges |

## 4. Open items (not closed by this audit)

Token revocation, refresh tokens, MFA and key rotation; row-level security as defence in depth; sandboxed parsing; document deletion/retention (erasure requests); external anchoring of the audit chain; `verify-full` database TLS with a mounted CA; container image vulnerability scanning (images never built here); independent penetration test; the two moderate frontend advisories.
