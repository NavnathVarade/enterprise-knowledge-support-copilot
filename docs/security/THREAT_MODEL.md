# Threat model (STRIDE), data classification, PII and retention

Status: written in Phase 32 against the code as it exists. Each mitigation names where it
lives; each gap is stated plainly. "Not implemented" means exactly that. This is a design
document, not a penetration test: **no external security assessment has been done.**

STRIDE: **S**poofing, **T**ampering, **R**epudiation, **I**nformation disclosure,
**D**enial of service, **E**levation of privilege.

## 1. Assets and trust boundaries

| Asset | Why it matters |
|---|---|
| Tenant documents, chunks, embeddings | Confidential business content; cross-tenant or cross-ACL leakage is the worst failure |
| Identity data (users, roles, groups, password hashes) | Account takeover, privilege escalation |
| JWT signing key, provider API keys, DB/Kafka credentials | Full impersonation or spend |
| Audit log | Evidence; must be tamper-evident |
| LLM / embedding spend | Direct money loss |

Boundaries: Internet → ingress → API; API → Postgres / Redis / Kafka / object store / LLM and
embedding providers (external, third-party); Kafka → ingestion worker; CI → registry → cluster.

## 2. STRIDE by component

### API (FastAPI)
| | Threat | Mitigation (implemented) | Gap / residual |
|---|---|---|---|
| S | Forged request identity / log ids | RS256 JWT only; request ids sanitised (`packages/security/http.py`) | — |
| T | Request smuggling via oversized bodies | Body-size limit before buffering (`BodyLimitMiddleware`), incl. chunked | No WAF; relies on ingress for header limits |
| R | Denied actions with no record | Denials audited (login, query, budget) | Not every read endpoint is audited |
| I | Error detail / stack traces; API schema recon | Generic 500 handler (tested); `/docs` and `/openapi.json` off outside dev/test; security headers; no `Server` header | Health `ready` reports dependency names |
| D | Request floods, huge bodies | Body limit; per-tenant LLM rate limit; per-tenant daily budget | **No per-IP/global request rate limit on non-LLM endpoints** (expected at the ingress; not configured here) |
| E | Missing role check | `require_role` dependency per route; tenant session binding | Authorisation is per route by convention, no central policy engine |

### Authentication
| | Threat | Mitigation | Gap |
|---|---|---|---|
| S | Credential stuffing / brute force | Redis failure throttle per account and per IP, checked before any hashing; lockout does not reveal account existence | A known account can be locked for one window by an attacker (availability trade-off); distributed attempts evade the IP counter; throttle degrades to allow if Redis is down (configurable, counted) |
| S | Account enumeration | Identical 401 for unknown tenant/user/bad password; one bcrypt check on every failure path (tested); over-long passwords handled identically | Tenant slugs are guessable by design (login needs them) |
| S | JWT algorithm confusion / `alg: none` | Algorithm pinned from an asymmetric-only allow-list; required claims enforced; tests forge both attacks | — |
| S | Stolen access token | 15-minute TTL | **No refresh tokens, no revocation list, no MFA, no SSO/OIDC.** Roles in a token are stale until expiry |
| I | Weak password storage | bcrypt with per-hash salt; 72-byte limit rejected, not truncated | No password policy (length/breach check) because no user-creation endpoint exists yet |

### Authorisation and tenancy
| | Threat | Mitigation | Gap |
|---|---|---|---|
| I/E | Cross-tenant read | Tenant id in every query; composite foreign keys; RLS read policies on analytics and usage tables; tenant-scoped sessions | RLS only protects non-owner DB roles; the app role that owns tables bypasses it, so explicit filters remain the primary control |
| I/E | Reading documents without ACL | ACL enforced inside the retrieval SQL, fail-closed; re-checked in context assembly; missing/draft versions look like "not found" | Classification labels (PUBLIC…RESTRICTED) are stored but §154 rules beyond ACL are not separately enforced |
| E | Privilege via admin APIs | `admin` / `security_auditor` roles on audit and usage endpoints; changes audited | Role assignment has no API or approval flow yet |

### Kafka
| | Threat | Mitigation | Gap |
|---|---|---|---|
| S/T | Forged or tampered events | SASL/SSL required in staging/production (settings refuse PLAINTEXT); the object key is derived from ids and never read from an event; stored object hash verified before parsing | No per-message signature; any principal with produce rights on the topic can publish (ACLs are the broker's job and are **not configured here**) |
| D | Poison messages | Bounded retries then DLQ; manual commits | `.retry` topics exist but nothing consumes them |
| I | Sensitive data in DLQ | DLQ holds raw base64 of the original message | Payloads include document ids and **file names, which may contain personal data**; 30-day topic retention is the only control |

### PostgreSQL
| | Threat | Mitigation | Gap |
|---|---|---|---|
| T | SQL injection | SQLAlchemy bound parameters; analytics are a validated intent → template builder (no model-written SQL) on a read-only role with statement timeout | — |
| T/R | Audit tampering | Append-only triggers + per-tenant SHA-256 hash chain + verify endpoint | A superuser can drop triggers and rewrite the chain; the head hash is not anchored outside the database |
| I | Data at rest / in transit | — | **TLS to Postgres and encryption at rest are deployment choices not enforced by the app** |

### Redis
Used for rate limits, login throttle, idempotency keys. Contains no document content. Keys
for logins are hashed (no emails). **Gaps:** no enforced AUTH/TLS; a Redis outage disables
rate limiting and login throttling by default (fail-open, metered).

### Document ingestion and file uploads
| | Threat | Mitigation | Gap |
|---|---|---|---|
| T | Malicious or disguised files | MIME allowlist; magic-byte/UTF-8 checks so content matches the claim; filenames validated (no separators, strict charset); object key never uses the filename | No antivirus/malware scan |
| D | Zip bombs, huge PDFs, text floods | DOCX: entry count, total expansion and per-entry ratio checked from the table of contents before any parse; PDF page cap; extracted-text cap; body limit before buffering | pypdf/python-docx run **in-process** in the worker (no sandbox); memory limit and pod isolation bound the damage; a parser CVE is still a code-execution risk |
| T | Path traversal | Unsafe archive paths rejected; nothing is extracted to disk | — |
| I | Document content becomes a prompt-injection carrier | See RAG below | — |

### Embeddings
Content is sent to a third-party provider (Voyage) over HTTPS. No PII masking before sending.
Provider outage → ingestion retries, then DLQ. Spend is metered (Phase 31).

### LLM and RAG
| | Threat | Mitigation | Gap |
|---|---|---|---|
| T | Prompt injection in documents | Heuristic detector quarantines suspicious chunks; sources escaped and delimited; per-request canary; output policy blocks prompt echo and exfiltration patterns; citations validated | Heuristic with documented misses; **real-model susceptibility is NOT YET MEASURED** |
| I | Leaking other tenants'/restricted data via the model | Only post-ACL, post-temporal chunks reach the prompt (tested with canary strings) | A prompt injection that survives can still make the model misreport what it *was* given |
| I | Sensitive content to the provider | Only authorised chunks are sent; no user emails or ids are put in prompts | No field-level PII masking; no rule that keeps RESTRICTED documents away from the external provider |
| D/E | Cost abuse | Context and output token caps, rate limit, daily budgets | Budget check can overshoot under concurrency (documented, Phase 31) |

### Admin APIs
Audit, usage and budget endpoints are admin-only, tenant-scoped, and audited where they
change state. No cross-tenant operator API exists (by design); there is also no break-glass
procedure documented yet.

### Kubernetes and Docker
| | Threat | Mitigation | Gap |
|---|---|---|---|
| E | Container breakout / privilege | Non-root uid 10001, read-only rootfs, all capabilities dropped, no privilege escalation, seccomp `RuntimeDefault`, Pod Security `restricted`, no service-account token mounted (all tested on the rendered manifests) | Never applied to a cluster; PSA admission untested |
| I/T | Lateral movement | Default-deny NetworkPolicy with scoped allows | Egress to the internet on 443 is open to any public address (provider IPs vary); unverified on a real CNI |
| I | Client IP spoofing (throttle bypass) | uvicorn started with `--no-proxy-headers`; app trusts exactly `HARDENING_TRUSTED_PROXY_HOPS` entries from the right | **The value must match the real ingress chain**; a wrong value either merges all users into one IP or lets clients pick theirs |
| I | `server-snippet` ingress annotation hiding `/metrics` | `/metrics` is also fail-closed in the app (token or 404) | Recent ingress-nginx versions disable snippet annotations by default, so the app-level control is the one to rely on |

### CI/CD and supply chain
| | Threat | Mitigation | Gap |
|---|---|---|---|
| T | Vulnerable dependency | Exact pins; `pip-audit` on the pins (clean when run here); the abandoned `python-jose`/`ecdsa` stack was removed in Phase 32 | **Hashes now recorded and verified** (`uv.lock`, uv migration); previously a re-uploaded package version is not detected |
| T | Leaked secret | gitleaks with a narrow allow-list; Settings refuse the committed test key outside dev/test | History scan has only run on a tree without git history here |
| T | Compromised image | Pinned base by tag (digest pass-through supported), SBOM + provenance, Trivy gate | **No image signing or admission verification** |
| T | Malicious workflow change | — | GitHub Actions use version tags, not commit SHAs; branch protection is a repository setting not managed here |

### Secrets
Never committed (`.env.example` / `.env.production.example` placeholders only; scanned). API keys and passwords are
`SecretStr`, redacted in logs. In Kubernetes they come from an out-of-band Secret or External
Secrets. **Gaps:** no automated rotation; the JWT key has no rotation/`kid` scheme, so rotating
it invalidates every session at once.

### Terraform and model artifacts
Terraform is **not built**, so there is nothing to assess. No models are self-hosted (provider
APIs only), so model-artifact supply chain does not apply today.

## 3. Data classification and authorisation rules (spec §154)

Documents carry one of `PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `RESTRICTED` plus an ACL.
Enforcement today is the **ACL**, evaluated in SQL and again at context assembly: a user sees
a document only if the ACL grants it (public-within-tenant, role, group or user), the version
is published and in effect, and the tenant matches. The classification label is metadata
and does not by itself change access. Restricted documents ingested without an ACL are
invisible to everyone (fail closed).

## 4. PII inventory and handling (spec §155)

| Data | Where | Collection / storage | Logging | Masking | Access |
|---|---|---|---|---|---|
| Email, display name, department | `users` | Provided by tenant admins; stored plaintext | Never logged; audit stores ids, not emails | Not masked in API | Authenticated tenant users; admin APIs |
| Password | `users.hashed_password` | bcrypt hash only | Never logged (redaction tested) | n/a | Never returned |
| Document content | `document_chunks`, object store | Uploaded by users; may contain personal data | Not logged | None | ACL-enforced |
| Query text | **Not stored**; audit keeps a 12-char hash | — | Not logged or traced (tested) | n/a | n/a |
| Answers | **Not stored** | — | — | — | — |
| Usage ledger | `usage_events` | user id, tenant, tokens, cost | ids only | n/a | Tenant admins |
| File names | Events, DLQ, document title | May contain personal data | In DLQ payloads | None | Operators with broker access |
| Customer/ticket data | analytics tables | Synthetic in this repo | — | — | Read-only role + RLS |

Rules: data is sent to the LLM/embedding provider only as authorised document chunks; user
identity fields are never put in prompts. **Not implemented:** field-level masking or
redaction of PII inside documents, data-subject export/deletion workflows, and
document deletion propagation to chunks, embeddings and object storage.

## 5. Retention (spec §156)

"Defined" is a policy decision; "enforced" means something actually deletes the data.

| Data | Proposed retention | Enforced today? |
|---|---|---|
| Documents and versions | Until deleted by the tenant | **No deletion feature exists** |
| Queries and answers | Not retained | Yes (not stored) |
| Audit log | 1 year online, then archive (suggested) | **No.** Append-only triggers forbid DELETE; retention needs time-partitioning and detaching partitions under a documented break-glass step |
| Usage ledger | 13 months (suggested) | No purge job |
| Outbox rows | Published rows 7 days | No purge job |
| Kafka events | 7 days main, 3 days retry | **Yes**, topic config from `create_topics` |
| DLQ | 30 days | **Yes**, topic config |
| Traces and logs | Set in the collector/log stack | Outside this repo |
| Feedback, evaluation results | Not stored by the application | n/a |

## 6. Highest-priority remaining risks

1. No refresh/revocation/MFA: a stolen token works for up to 15 minutes with stale roles.
2. Parsers run in-process (no sandbox) on attacker-supplied files.
3. No document deletion or retention enforcement (privacy obligations cannot be met yet).
4. TLS/AUTH for Postgres and Redis and Kafka ACLs are not enforced by this codebase.
5. Prompt-injection defences are heuristic and unmeasured against a real model.
6. Supply chain: no dependency hashes, no image signing, Actions not pinned to SHAs.
