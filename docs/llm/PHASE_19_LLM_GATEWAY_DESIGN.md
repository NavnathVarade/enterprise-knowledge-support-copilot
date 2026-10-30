# Phase 19 — LLM Gateway (Spec §37–38)

## Modules (`packages/llm/`)
| File | Role |
|---|---|
| `base.py` | `LLMProvider` protocol, `LLMRequest/Response`, provider error family |
| `anthropic_provider.py` | Real HTTP client for the Anthropic Messages API (no retries inside) |
| `gateway.py` | Rate limit → context pre-check → bounded retry → fallback → controlled failure |
| `pricing.py` | Cost accounting from configured prices only |
| `rate_limit.py` | Per-tenant Redis fixed-window limiter |

## Behavior
1. **Rate limit** per tenant (`llm:rl:{tenant}:{window}`); tenants never share counters.
2. **Context pre-check**: estimated input + max output tokens > `max_context_tokens` → `LLMContextOverflow` before any provider call.
3. **Retries**: only `ProviderTransientError` (timeouts, network, 5xx/529, 429). Exponential backoff with full jitter, capped at `retry_max_delay_seconds`; a provider `Retry-After` is honored but capped. Total attempts = 1 + `max_retries`.
4. **Per-attempt timeout** via `asyncio.wait_for`; maps to `LLMTimeout`.
5. **Fallback** (if configured): after primary retries are exhausted, or on a primary auth error. NOT used for `ProviderInvalidRequest` (a different provider won't fix a bad request).
6. **Failure**: controlled domain errors (`LLMUnavailable`, `LLMTimeout`, `LLMInvalidResponse`, `LLMContextOverflow`). The gateway never returns a made-up answer.
7. **Structured output**: `complete_json` parses a JSON object (strips one code fence) or raises `LLMInvalidResponse`. No silent repair.
8. **Accounting**: each `LLMResult` carries tokens, `cost_usd`, `attempts`, `fallback_used`.

## Decisions
- **No built-in price table.** Prices change and a baked-in number is an unverifiable claim. A model without configured pricing returns `cost_usd=None`, never 0.
- **Retry lives only in the gateway**, so there is one place that bounds total attempts (providers do not retry internally).
- **Rate limiter fails open by default** on Redis errors (logged); `fail_open=False` makes it fail closed. Redis is not a source of truth (§51).
- Prompts are never logged (the logger redacts `prompt`); log fields carry error class and provider names only.

## Tests (33)
Retry count/bound, jitter + cap, Retry-After cap, no retry on invalid request/auth, fallback paths, both-fail, timeout, context overflow, cost with/without price, JSON parsing, request shape, HTTP status mapping, per-tenant limit, fail-open/closed.

## Not verified / not built (honest)
- **Anthropic provider is NOT network-verified** (mock transport only). Request shape follows the documented API; confirm with a real key before relying on it.
- Token counts for pre-checks use the ~4 chars/token heuristic; the provider's reported usage is authoritative for accounting.
- Only one provider implementation exists; the fallback can be another Anthropic model. A second vendor needs a new `LLMProvider`.
- No streaming, no circuit breaker (repeated outages still retry every request), no prompt versioning yet (next with generation, §36).
- Fixed-window limiter allows up to 2x burst at window edges.
- Latency/throughput/cost under real traffic: NOT YET MEASURED.
