# Phase 23 — Support & Incident Investigation (Spec §42–43)

Deterministic evidence gathering: **no LLM is involved**, so every item in a report traces to a database row or a retrieved chunk. (A later phase may have a model *narrate* a report with citations; it must never invent evidence.)

## Customer investigation — `investigate_customer_issue`
`customer → tickets → ticket events → overlapping incidents → preceding deployments → knowledge articles / error documentation → ranked leads`

| Output | Content |
|---|---|
| `tickets`, `incidents`, `deployments` | Structured facts only (ids, service, status, times, error code) — no free text |
| `candidates` | Ranked temporal-correlation leads, each with the rule that fired (`reason`), `strength` (strong/moderate) and linked ticket/incident ids |
| `timeline` | Merged, time-sorted events (ticket opened/resolved/event, incident start/end, deployment) |
| `knowledge` | References from ACL-enforced retrieval (`error_documentation` via the ticket's error code, `knowledge_article` via service name) |
| `truncated` | Per-source flags when a cap was hit (50 tickets, 200 events, 100 incidents, 200 deployments) |
| `disclaimer` | "Candidates are temporal correlations … not confirmed root causes." |

### Correlation rules (`correlation.py`, pure and unit-tested)
- An incident overlaps a ticket when the services match and the windows intersect, with a 1-hour grace; an open-ended incident runs until `now`.
- Incident is **strong** if it overlaps ≥2 of the customer's tickets or is SEV1/SEV2; otherwise **moderate**.
- Deployments are considered within a 24h lookback *before* the incident start (or the ticket opening). **Strong** if within 6h and (failed/rolled back, or it precedes an overlapping incident); otherwise **moderate**. Each deployment is listed once.
- Strength is a stated rule, **not a probability**, and not a statistical model.

## Incident analysis — `analyze_incidents`
Unresolved incidents; recurring patterns (same service + root-cause category, ≥2 in the window); "what changed before" (deployments in the 24h before each incident start); optional per-service filter. Default window 90 days, maximum 366.

## Security
- Requires a role from `allowed_roles` (default `support_agent`, `admin`) → else `PermissionDenied`. Tenant comes only from the authenticated `Identity`.
- Every repository query filters `tenant_id` explicitly. A customer id from another tenant yields `ResourceNotFound` (same answer as a nonexistent id — no existence leak). Tests seed an identical scenario in a second tenant and prove none of its rows appear.
- Knowledge references use `reranked_hybrid_search_authorized`; a test shows a restricted document matching the same error code is never referenced.
- Time windows must be timezone-aware, ordered, and ≤366 days.

## Tests (24): 14 unit (correlation) + 10 integration on real Postgres
Overlap boundaries/grace/open-ended, lookback window and ordering, strong/moderate rules, dedup, ranking, no-evidence cases; end-to-end scenario with timeline, filters, role checks, cross-tenant isolation, window validation, unresolved/recurring/preceding-change analysis, ACL-enforced knowledge. Full suite after a fresh-DB alembic up/down/up: see README.

## Not verified / not built (honest)
- **Correlation is not causation.** The rules produce leads. Their usefulness on real incidents is NOT YET MEASURED; thresholds (1h grace, 24h lookback, 6h "near") are defaults, not tuned.
- These workflows read the owner connection with explicit tenant filters; they do **not** run under the read-only role/RLS (that protects the Phase 22 aggregate path). Moving them behind RLS is a possible hardening.
- No HTTP endpoint exposes the workflows yet, and the router's `SUPPORT_ANALYSIS` / `INCIDENT_ANALYSIS` decisions are not wired to them (orchestrator phase).
- No natural-language narrative, no customer lookup by name/email (ids only, by design: the schema holds no PII), no service-dependency graph (an incident in an upstream service is not linked to a downstream ticket), no `affected_customers` join and no ingestion of real ticket/incident/deployment data.
- Knowledge lookup uses the deterministic embedding and lexical reranker in tests; semantic quality with real providers is NOT YET MEASURED.
