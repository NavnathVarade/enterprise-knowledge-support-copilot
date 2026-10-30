import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, parseRetryAfter } from "./client";

function client(fetchImpl: typeof fetch, token: string | null = "tok", onUnauthorized = vi.fn()) {
  return { api: new ApiClient({ getToken: () => token, onUnauthorized, fetchImpl }), onUnauthorized };
}
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

describe("ApiClient", () => {
  it("sends the bearer token, omits cookies and disables caching", async () => {
    const f = vi.fn().mockResolvedValue(ok([]));
    await client(f as unknown as typeof fetch).api.listDocuments();
    const [url, init] = f.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/documents");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok");
    expect(init.credentials).toBe("omit");
    expect(init.cache).toBe("no-store");
  });

  it("never attaches a token to the login call", async () => {
    const f = vi.fn().mockResolvedValue(ok({ access_token: "a", token_type: "bearer", expires_in_seconds: 1 }));
    await client(f as unknown as typeof fetch).api.login("acme", "a@b.co", "pw");
    const init = (f.mock.calls[0] as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>)["Authorization"]).toBeUndefined();
    expect(JSON.parse(init.body as string)).toEqual({ tenant_slug: "acme", email: "a@b.co", password: "pw" });
  });

  it("maps the server error envelope, including the request id", async () => {
    const body = { error: { code: "BUDGET_EXCEEDED", message: "Daily budget reached.", request_id: "r-1" } };
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 429, headers: { "retry-after": "30" } }));
    const err = await client(f as unknown as typeof fetch).api.query("x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect([e.status, e.code, e.message, e.requestId, e.retryAfterSeconds]).toEqual([429, "BUDGET_EXCEEDED", "Daily budget reached.", "r-1", 30]);
    expect(e.transient).toBe(true);
  });

  it("never surfaces a non-JSON error body (e.g. a gateway HTML page)", async () => {
    const f = vi.fn().mockResolvedValue(new Response("<html>nginx secret internals</html>", { status: 502 }));
    const e = (await client(f as unknown as typeof fetch).api.listDocuments().catch((x: unknown) => x)) as ApiError;
    expect(e.message).toBe("The server returned 502.");
    expect(e.message).not.toContain("nginx");
  });

  it("signs out on 401 for authenticated calls but not for a failed login", async () => {
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "INVALID_CREDENTIALS", message: "no" } }), { status: 401 }));
    const a = client(f as unknown as typeof fetch);
    await a.api.listDocuments().catch(() => undefined);
    expect(a.onUnauthorized).toHaveBeenCalledTimes(1);
    const b = client(f as unknown as typeof fetch);
    await b.api.login("t", "a@b.co", "bad").catch(() => undefined);
    expect(b.onUnauthorized).not.toHaveBeenCalled();
  });

  it("turns network failures and timeouts into typed transient errors", async () => {
    const net = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    const e1 = (await client(net as unknown as typeof fetch).api.listDocuments().catch((x: unknown) => x)) as ApiError;
    expect(e1.code).toBe("NETWORK_ERROR");
    expect(e1.transient).toBe(true);

    vi.useFakeTimers();
    const hang = vi.fn((_u: string, init: RequestInit) =>
      new Promise<Response>((_res, rej) => {
        init.signal?.addEventListener("abort", () => { rej(new DOMException("aborted", "AbortError")); });
      }),
    );
    const p = new ApiClient({ getToken: () => "t", onUnauthorized: vi.fn(), fetchImpl: hang as unknown as typeof fetch, timeoutMs: 1000 })
      .listDocuments()
      .catch((x: unknown) => x);
    await vi.advanceTimersByTimeAsync(1001);
    expect(((await p) as ApiError).code).toBe("TIMEOUT");
    vi.useRealTimers();
  });

  it("uploads multipart without forcing a content type", async () => {
    const f = vi.fn().mockResolvedValue(ok({ id: "1", title: "a.txt", status: "uploaded", content_hash: "h" }));
    await client(f as unknown as typeof fetch).api.uploadDocument(new File(["hi"], "a.txt", { type: "text/plain" }));
    const init = (f.mock.calls[0] as [string, RequestInit])[1];
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });

  it("clamps and validates Retry-After", () => {
    expect(parseRetryAfter("5")).toBe(5);
    expect(parseRetryAfter("999999")).toBe(3600);
    expect(parseRetryAfter("-1")).toBeNull();
    expect(parseRetryAfter("soon")).toBeNull();
    expect(parseRetryAfter(null)).toBeNull();
  });
});

describe("ApiClient: signup, health and cancellation", () => {
  it("signs up without a token and posts exactly the documented fields", async () => {
    const f = vi.fn().mockResolvedValue(ok({ access_token: "a", token_type: "bearer", expires_in_seconds: 1, tenant_slug: "acme" }));
    const input = { organization_name: "Acme", tenant_slug: "acme", display_name: "Ada", email: "a@b.co", password: "x".repeat(12) };
    await client(f as unknown as typeof fetch).api.signup(input);
    const [url, init] = f.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/signup");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Authorization"]).toBeUndefined();
    expect(JSON.parse(init.body as string)).toEqual(input);
  });

  it("does not treat a 401 from signup as an expired session", async () => {
    const f = vi.fn().mockResolvedValue(new Response("{}", { status: 401 }));
    const { api, onUnauthorized } = client(f as unknown as typeof fetch);
    await expect(api.signup({ organization_name: "A", tenant_slug: "aaa", display_name: "A", email: "a@b.co", password: "x" })).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("returns the readiness report for both 200 and 503 instead of throwing", async () => {
    const body = { status: "not_ready", checks: { database: "unavailable: TimeoutError", kafka: "disabled" } };
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 503 }));
    const r = await client(f as unknown as typeof fetch).api.ready();
    expect(r.httpStatus).toBe(503);
    expect(r.report.checks["database"]).toBe("unavailable: TimeoutError");
  });

  it("never renders a gateway HTML page returned for readiness", async () => {
    const f = vi.fn().mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }));
    const err = await client(f as unknown as typeof fetch).api.ready().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).not.toContain("html");
    expect((err as ApiError).status).toBe(502);
  });

  it("reports a caller cancellation as CANCELLED, not as a timeout", async () => {
    const f = vi.fn((_u: string, init: RequestInit) =>
      new Promise<Response>((_res, rej) => {
        init.signal?.addEventListener("abort", () => {
          rej(new DOMException("aborted", "AbortError"));
        });
      }),
    );
    const c = new AbortController();
    const p = client(f as unknown as typeof fetch).api.query("hi", c.signal);
    c.abort();
    await expect(p).rejects.toMatchObject({ code: "CANCELLED" });
  });
});
