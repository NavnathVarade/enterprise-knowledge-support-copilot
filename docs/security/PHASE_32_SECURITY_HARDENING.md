# Phase 32 — Security hardening

Companion: `THREAT_MODEL.md` (STRIDE, data classification, PII inventory, retention, remaining risks).

## 1. Findings from the audit and what changed

| # | Finding | Change |
|---|---|---|
| 1 | Login had no brute-force protection; response timing and a 422 for over-long passwords revealed whether an account exists | Redis throttle per account and per IP (hashed keys, 429 `LOGIN_THROTTLED`); a dummy bcrypt check on every failure path; over-long passwords return 401 |
| 2 | No security headers; `/docs`, `/openapi.json` public in production; no request-size limit | Pure-ASGI `SecurityHeadersMiddleware` (nosniff, DENY framing, CSP, no-store, HSTS in staging/production only); docs off unless dev/test or `HARDENING_DOCS_ENABLED=true`; `BodyLimitMiddleware` (413 by Content-Length and by streamed byte count; upload route gets its own cap) |
| 3 | Client `X-Request-Id` reflected and stored in a 64-char column (long ids would silently break usage inserts) | Sanitised to `[A-Za-z0-9._:-]{1,64}`, else a fresh uuid4 |
| 4 | `python-jose` / `ecdsa` carried advisories | Replaced by PyJWT with required claims and an asymmetric-only algorithm allow-list (`alg=none` and HS256-with-public-key confusion are tested) |
| 5 | Uvicorn trusted `X-Forwarded-For` from any peer, which would defeat the IP throttle | `--no-proxy-headers`; client IP derived from the n-th XFF entry from the right via `HARDENING_TRUSTED_PROXY_HOPS` (default 0) |
| 6 | The committed test private key could be configured in staging/production | Settings refuse it (sha256 fingerprint check) |
| 7 | Seed script created a known-password admin with no guard | Refuses unless `ENVIRONMENT` is development or test |
| 8 | Upload validation trusted the claimed MIME type; no zip-bomb or page limits | Content sniffing, encrypted-PDF rejection, page cap, DOCX entry/expansion/ratio/path checks, extracted-text cap |
| 9 | Bandit findings, bare asserts in production code, literal hidden characters in source | Fixed; bandit now gates CI |
| 10 | Dependency and secret scanning was non-blocking | CI `security-scan` job: pip-audit on pins, bandit, gitleaks (blocking) |

## 2. Configuration (`HARDENING_*`)

`DOCS_ENABLED` (unset = dev/test only), `MAX_REQUEST_BODY_BYTES` (1 MiB), `UPLOAD_OVERHEAD_BYTES`,
`TRUSTED_PROXY_HOPS` (0–5), `LOGIN_MAX_FAILURES_PER_ACCOUNT` (5), `LOGIN_MAX_FAILURES_PER_IP` (30),
`LOGIN_WINDOW_SECONDS` (900), `LOGIN_THROTTLE_FAIL_OPEN` (true), `HSTS_MAX_AGE_SECONDS`.
Ingestion limits: `INGESTION_MAX_PDF_PAGES`, `MAX_ZIP_ENTRIES`, `MAX_ZIP_UNCOMPRESSED_BYTES`,
`MAX_ZIP_COMPRESSION_RATIO`, `MAX_EXTRACTED_CHARS`. Kubernetes ConfigMap sets hops=1 and docs off.

**Operator action:** `TRUSTED_PROXY_HOPS=1` assumes exactly one trusted proxy (the ingress) in front
of the API. If a CDN or extra load balancer sits in front, raise it; if the value is too low every
client appears as the proxy's IP and the per-IP limit becomes a global limit; too high lets a
client spoof its IP. Verify against the real chain.

## 3. Failure behaviour

- Redis down: login throttle follows `LOGIN_THROTTLE_FAIL_OPEN` (default allow, counted in
  `ekc_login_throttle_errors_total{kind}`). Set false to fail closed (login unavailable when Redis is).
- Throttle denials counted in `ekc_login_throttled_total{kind}`.
- A successful login clears the account counter, not the IP counter.
- A deliberate attacker can lock out a known account for the window (account-lockout DoS); the
  per-IP counter and the window bound this but do not remove it.

## 4. Verification

| Item | Status |
|---|---|
| bandit (`-c pyproject.toml`) over packages, apps, rag, scripts, data, migrations | **Run: 0 issues** |
| pip-audit on the production pins (then `requirements/constraints.txt`, now exported from `uv.lock`) | **Run: no known vulnerabilities** (advisory DB as of the run date; not a guarantee) |
| gitleaks on the repo | **Run: clean**; negative control (a realistic fake token) **detected** |
| hadolint | **Run: clean** |
| Rebuilt container (`ekc:local-verify`, substitute base) | **Run:** headers present, no `Server` header, throttle engaged on 6th attempt (401×5 then 429), 2 MB body → 413, `jwt` present and `jose` absent |
| Real-HTTP integration tests (Postgres, Redis) | **Run:** throttle, no enumeration oracle, IP spraying, XFF spoofing, Redis outage both modes, one bcrypt per failure path, 413 variants, docs gating, upload rejection |
| Real zip bomb, encrypted and multi-page PDFs | **Run** in unit tests |
| CI `security-scan` job on GitHub | **Not run** (the same tools ran locally) |
| Throttle behind a real ingress / XFF chain | **Not verified** |
| Penetration test, fuzzing, DAST | **Not done** |
| Timing equalisation | Tested as "one bcrypt per path", **not** statistically measured over the network |

## 5. Known gaps (not fixed in this phase)

No refresh tokens, token revocation or MFA; JWT has no `kid`/key rotation; document parsers run
in-process without a sandbox; no antivirus scan; no per-IP rate limit on non-login endpoints; no
document deletion or retention enforcement (the audit log is append-only by trigger); TLS/AUTH
for Postgres and Redis and Kafka ACLs are deployment concerns not enforced by code; no PII
masking; (dependency hashes: closed by the uv migration, `uv.lock`); images unsigned; GitHub Actions not pinned to commit SHAs.

## 6. Tests

`test_security_http.py` (29), `test_security_hardening_policy.py` (7),
`test_upload_content_validation.py` (24), `integration/test_security_hardening.py` (23); changes to
the prompt-injection and ingestion-pipeline tests.

Full suite: **772 passed** (with `KUSTOMIZE_BIN` set, so the kustomize-based tests ran); ruff and mypy
strict clean; migrations upgrade → downgrade → upgrade on a fresh database. A pre-existing Kafka test
fixture that reused the committed test key in a production-mode `Settings` was caught by the new
guard and now generates a fresh key.
