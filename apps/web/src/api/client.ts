import type {
  AuditEvent,
  Budget,
  BudgetInput,
  ChainVerification,
  DocumentSummary,
  DocumentUploadResponse,
  LoginResponse,
  QueryResponse,
  ReadyReport,
  SignupInput,
  SignupResponse,
  UsageSummary,
} from "./types";

/** A failed API call, carrying only what the server's error envelope exposes. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
    readonly retryAfterSeconds: number | null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** True for overload/outage responses the user can simply retry. */
  get transient(): boolean {
    return this.status === 429 || this.status === 503 || this.status === 504 || this.status === 0;
  }
}

export interface ClientOptions {
  getToken: () => string | null;
  onUnauthorized: () => void;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
  timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 30_000;
const QUERY_TIMEOUT_MS = 90_000; // retrieval + LLM can legitimately take longer

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

export function parseRetryAfter(value: string | null): number | null {
  if (value === null) return null;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.min(n, 3600) : null;
}

async function toApiError(res: Response): Promise<ApiError> {
  let code = "HTTP_ERROR";
  let message = `The server returned ${String(res.status)}.`;
  let requestId = res.headers.get("x-request-id");
  try {
    const body: unknown = await res.json();
    if (isRecord(body) && isRecord(body["error"])) {
      const e = body["error"];
      if (typeof e["code"] === "string") code = e["code"];
      if (typeof e["message"] === "string") message = e["message"];
      if (typeof e["request_id"] === "string") requestId = e["request_id"];
    } else if (isRecord(body) && res.status === 422) {
      code = "VALIDATION_ERROR";
      message = "The request was not valid. Check the fields and try again.";
    }
  } catch {
    // non-JSON body (e.g. a gateway error page): keep the generic message, never render it
  }
  return new ApiError(res.status, code, message, requestId, parseRetryAfter(res.headers.get("retry-after")));
}

export class ApiClient {
  private readonly fetchImpl: typeof fetch;
  private readonly baseUrl: string;

  constructor(private readonly opts: ClientOptions) {
    this.fetchImpl = opts.fetchImpl ?? ((...a) => fetch(...a));
    this.baseUrl = opts.baseUrl ?? "/api/v1";
  }

  private async request<T>(
    method: string,
    path: string,
    init: { json?: unknown; form?: FormData; auth?: boolean; timeoutMs?: number; signal?: AbortSignal } = {},
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = init.auth === false ? null : this.opts.getToken();
    if (token !== null) headers["Authorization"] = `Bearer ${token}`;
    let body: BodyInit | undefined;
    if (init.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(init.json);
    } else if (init.form !== undefined) {
      body = init.form; // the browser sets the multipart boundary
    }
    const controller = new AbortController();
    // Caller cancellation (e.g. leaving the page) aborts the same request as the timeout does.
    init.signal?.addEventListener("abort", () => {
      controller.abort();
    });
    const timer = setTimeout(() => {
      controller.abort();
    }, init.timeoutMs ?? this.opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    let res: Response;
    try {
      res = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers,
        ...(body !== undefined ? { body } : {}),
        signal: controller.signal,
        credentials: "omit", // bearer tokens only; never ambient cookies
        cache: "no-store",
      });
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError";
      const cancelled = aborted && init.signal?.aborted === true;
      throw new ApiError(
        0,
        cancelled ? "CANCELLED" : aborted ? "TIMEOUT" : "NETWORK_ERROR",
        cancelled
          ? "The request was cancelled."
          : aborted
            ? "The request took too long. Try again."
            : "Could not reach the server.",
        null,
        null,
      );
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      const error = await toApiError(res);
      if (res.status === 401 && init.auth !== false) this.opts.onUnauthorized();
      throw error;
    }
    return (await res.json()) as T;
  }

  signup(input: SignupInput): Promise<SignupResponse> {
    return this.request("POST", "/auth/signup", { json: input, auth: false });
  }

  /** Liveness: the process answers. Says nothing about dependencies. */
  live(): Promise<{ status: string }> {
    return this.request("GET", "/health/live", { auth: false, timeoutMs: 8_000 });
  }

  /**
   * Readiness. The API answers 503 *with a report body* when not ready; that is a result, not a
   * transport failure, so it is returned instead of thrown. Anything else non-OK still throws.
   */
  async ready(): Promise<{ httpStatus: number; report: ReadyReport }> {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, 8_000);
    let res: Response;
    try {
      res = await this.fetchImpl(`${this.baseUrl}/health/ready`, {
        method: "GET",
        headers: { Accept: "application/json" },
        signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
      });
    } catch {
      throw new ApiError(0, "NETWORK_ERROR", "Could not reach the server.", null, null);
    } finally {
      clearTimeout(timer);
    }
    if (res.status === 200 || res.status === 503) {
      try {
        const body: unknown = await res.json();
        if (isRecord(body) && typeof body["status"] === "string" && isRecord(body["checks"])) {
          const checks: Record<string, string> = {};
          for (const [k, v] of Object.entries(body["checks"])) if (typeof v === "string") checks[k] = v;
          return { httpStatus: res.status, report: { status: body["status"], checks } };
        }
      } catch {
        // fall through to the generic error below
      }
    }
    throw await toApiError(res);
  }

  login(tenantSlug: string, email: string, password: string): Promise<LoginResponse> {
    return this.request("POST", "/auth/login", {
      json: { tenant_slug: tenantSlug, email, password },
      auth: false,
    });
  }

  listDocuments(): Promise<DocumentSummary[]> {
    return this.request("GET", "/documents");
  }

  uploadDocument(file: File): Promise<DocumentUploadResponse> {
    const form = new FormData();
    form.append("file", file);
    return this.request("POST", "/documents", { form, timeoutMs: 120_000 });
  }

  query(text: string, signal?: AbortSignal): Promise<QueryResponse> {
    return this.request("POST", "/query", {
      json: { query: text },
      timeoutMs: QUERY_TIMEOUT_MS,
      ...(signal !== undefined ? { signal } : {}),
    });
  }

  usageSummary(days: number): Promise<UsageSummary> {
    return this.request("GET", `/usage/summary?days=${String(days)}`);
  }

  getBudget(): Promise<Budget> {
    return this.request("GET", "/usage/budget");
  }

  putBudget(input: BudgetInput): Promise<Budget> {
    return this.request("PUT", "/usage/budget", { json: input });
  }

  auditEvents(limit: number, beforeSeq?: number): Promise<AuditEvent[]> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (beforeSeq !== undefined) q.set("before_seq", String(beforeSeq));
    return this.request("GET", `/audit?${q.toString()}`);
  }

  verifyAudit(): Promise<ChainVerification> {
    return this.request("GET", "/audit/verify");
  }
}
