// Mirrors the FastAPI response models (apps/api/app/routes/*). The server is the authority:
// these types document the contract, they do not validate it.

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in_seconds: number;
}

export interface DocumentSummary {
  id: string;
  title: string;
  status: string;
}

export interface DocumentUploadResponse extends DocumentSummary {
  content_hash: string;
}

export interface Citation {
  source_id: string;
  document_id: string;
  document_title: string;
  document_version: number;
  section: string | null;
  page: number | null;
}

export interface RouteInfo {
  route: string;
  confidence: number;
  classifier_version: string;
  reason: string;
  fallback_used: boolean;
}

export interface AnalyticsResult {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
}

export interface QueryResponse {
  status: string;
  message: string;
  route: RouteInfo;
  citations: Citation[];
  analytics: AnalyticsResult | null;
  payload: Record<string, unknown> | null;
  trace: Record<string, unknown>;
}

export interface AuditEvent {
  seq: number;
  occurred_at: string;
  actor_user_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  request_id: string | null;
  details: Record<string, unknown>;
}

export interface ChainVerification {
  ok: boolean;
  checked: number;
  first_bad_seq: number | null;
  reason: string | null;
}

export interface UsageDaily {
  day: string;
  kind: string;
  events: number;
  input_tokens: number;
  output_tokens: number;
  known_cost_usd: string;
  unpriced_events: number;
  unreported_token_events: number;
}

export interface UsageSummary {
  since: string;
  daily: UsageDaily[];
  query_cost: {
    queries_counted: number;
    queries_excluded_unpriced: number;
    average_cost_usd: string | null;
  };
  top_documents: {
    document_id: string;
    events: number;
    input_tokens: number;
    known_cost_usd: string;
    unpriced_events: number;
  }[];
  note: string;
}

export interface Budget {
  daily_token_limit: number | null;
  daily_cost_usd_limit: string | null;
  source: string;
  used_llm_tokens_today: number;
  known_cost_usd_today: string;
}

export interface BudgetInput {
  daily_token_limit: number | null;
  daily_cost_usd_limit: number | null;
}

export interface SignupInput {
  organization_name: string;
  tenant_slug: string;
  display_name: string;
  email: string;
  password: string;
}

export interface SignupResponse extends LoginResponse {
  tenant_slug: string;
}

/** `GET /health/ready`: status is ready | degraded | not_ready; each check is "ok", "disabled",
 * "degraded: <reason>" or "unavailable: <reason>". */
export interface ReadyReport {
  status: string;
  checks: Record<string, string>;
}
