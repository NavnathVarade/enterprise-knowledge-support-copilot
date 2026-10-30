# Phase 31 — Cost monitoring and budgets (Spec §106, §107)

## 1. Scope

Before this phase the only cost signal was a process-wide Prometheus counter. Nothing was
persisted per tenant, nothing could stop a tenant from spending without limit, and there was
no cost-per-query or cost-per-document view. This phase adds a usage ledger, per-tenant daily
budgets enforced before each LLM call, and reporting.

## 2. Design

**Meter at the gateway.** Every LLM call in the system passes through `LLMGateway`
(answers, document comparison, the analytics planner), so metering and budget enforcement
live there; no call site can forget them. Who the call is for (operation, user, request id,
document) travels in a `ContextVar` (`packages/usage/context.py`), set by the query route and
the ingestion stage.

**Ledger (`usage_events`, migration 0007).** One row per successful LLM call and one per
ingestion embedding run. Columns: tenant, time, kind (`llm`/`embedding`), operation, provider,
model, `input_tokens` (nullable), `output_tokens`, `cost_usd` (nullable), user, document,
request id. RLS SELECT policy as in the analytics tables.

**Unknown is never zero.**
- `cost_usd` is NULL when no price is configured for that model.
- `input_tokens` is NULL when the embedding provider did not report usage. Nothing is
  estimated: the deterministic provider reports nothing, Voyage reports `usage.total_tokens`,
  and if any batch of a document omits usage the whole document is recorded as unreported
  rather than as a partial sum.
- Reports expose `unpriced_events` and `unreported_token_events` next to every total, and
  cost-per-query excludes any query containing an unpriced call (and says how many).

**Prices.** Operator-supplied (`LLM_*_USD_PER_MILLION_TOKENS`,
`LLM_FALLBACK_*`, `EMBEDDING_USD_PER_MILLION_TOKENS`); they are not fetched from anywhere and
go stale when the provider changes pricing. **Fixed in this phase:** the fallback model used
to be priced at the primary's rate, silently mis-costing every fallback call. It now has its
own price or is unpriced.

**Budgets.** `tenant_budgets(tenant_id, daily_token_limit, daily_cost_usd_limit)`; the day is
the UTC calendar day.
- A tenant with a row uses it exactly (NULL = unlimited for that dimension); a tenant without
  one uses `USAGE_DEFAULT_DAILY_*`. Both unset = unlimited (the default).
- Token limit counts LLM tokens (input + output). Cost limit counts known cost across LLM and
  embeddings. With no prices configured a cost limit can never trigger: use the token limit.
- The guard runs before the provider is called, so a refused request costs nothing. It raises
  `BudgetExceeded` → HTTP 429 `BUDGET_EXCEEDED`; the denial is audited and counted in
  `ekc_budget_rejections_total{limit}`.
- Embedding spend is metered but never blocked: refusing mid-ingestion would create retry
  storms and half-indexed documents.

**Failure behaviour.** Recording is best effort: a failed write is logged and counted
(`ekc_usage_record_failures_total{kind}`) and never fails the user's request, so the ledger
can under-count by exactly that counter. A failing budget check follows
`USAGE_BUDGET_FAIL_OPEN` (default: allow, consistent with the rate limiter).

**Query cost control (§107).** The existing router already sends non-generative routes to
SQL/no-LLM paths, and `max_context_tokens`, `max_output_tokens`, the rate limit and now the
budget bound the rest. No new routing logic was added in this phase.

## 3. API (tenant-scoped, `admin` role)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/usage/summary?days=1..90` | Per day and kind: events, tokens, known cost, unpriced and unreported counts; average cost per query; top 20 documents by embedding cost |
| `GET /api/v1/usage/budget` | Effective limits, whether they come from the tenant or the default, and today's use |
| `PUT /api/v1/usage/budget` | Replace this tenant's limits (null = unlimited); audited in the same transaction |

## 4. Known limitations

- **Overshoot.** The check happens before the call and the spend is recorded after it, so N
  concurrent requests that all pass the check can exceed the limit by up to N calls. The
  overshoot is bounded by concurrency, not unbounded, and the next request is refused.
- **One extra query per LLM call** (budget row, plus a day aggregate when a limit exists)
  over the `(tenant_id, occurred_at)` index. Its cost under load is NOT YET MEASURED; a short
  per-tenant cache is the obvious fix if it matters.
- **Not metered:** query-time embeddings (`embed_query`) and rerank calls (Voyage rerank). The
  query embedding is one short string per question; the rerank client wrapper reads only the
  result list and discards any usage in the response. Both are real spend that this ledger does not include.
- **Failed provider attempts** that are retried or fall back are billed by the provider but
  recorded only as the one successful call.
- **No ledger retention or archival**; the table grows without bound.
- **No alert rules or dashboard panels** for budget rejections or record failures yet; the
  metrics exist.
- Prices and limits are plain environment/DB values; there is no currency handling beyond USD.

## 5. Tests

- `tests/unit/test_usage_pricing.py` — per-model pricing (fallback never borrows), default
  limits, UTC day boundary across timezones, context scoping.
- `tests/unit/test_voyage_provider.py` — token sums across batches, any omitted/malformed
  usage ⇒ unreported, plain `embed_batch` unchanged.
- `tests/integration/test_usage_and_budgets.py` (real Postgres, real HTTP) — gateway records
  with context and price; NULL cost when unpriced; recorder failure does not break the call
  and is counted; tenant isolation; token budget blocks only that tenant and the provider is
  never called when refused; cost budget counts known cost only and includes embeddings;
  embeddings do not count toward the token limit; yesterday's spend ignored; default vs
  tenant row; fail-open vs fail-closed; summary arithmetic including excluded unpriced
  queries; ingestion records reported tokens with the document, and records unknown (not
  zero) when the provider reports nothing; admin-only API, validation, audit trail, tenant
  scoping; and `/api/v1/query` metering two answers then returning 429 `BUDGET_EXCEEDED`
  without a model call.
- Metrics policy test extended (new `limit`/`kind` labels are bounded and allow-listed).

Full suite: **689 passed**; ruff and mypy strict clean; migrations upgrade → downgrade →
upgrade on a fresh database.

## 6. Not verified

- No real provider run: token counts and prices were exercised with scripted providers and a
  mocked Voyage transport. Real billed amounts were never compared with the ledger.
- Behaviour under concurrent load (overshoot size, guard query cost) is NOT YET MEASURED.
- Migration 0007 was applied only to a fresh database, not to one with production-sized data
  (it only creates tables, so it should be fast, but that is untested at scale).
