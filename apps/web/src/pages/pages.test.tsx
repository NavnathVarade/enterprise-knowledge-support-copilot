import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { json, renderApp, signInAs } from "../test/helpers";
import type { Handler } from "../test/helpers";
import { makeToken } from "../test/helpers";
import { AuditPage } from "./AuditPage";
import { DocumentsPage } from "./DocumentsPage";
import { LoginPage } from "./LoginPage";
import { QueryPage } from "./QueryPage";

const err = (status: number, code: string, message: string, headers: Record<string, string> = {}) =>
  json({ error: { code, message, request_id: "req-9" } }, { status, headers });

describe("login", () => {
  const fill = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.type(screen.getByLabelText("Organization"), "acme");
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
  };

  it("signs in and lands on the app", async () => {
    const handler: Handler = (url) =>
      url.endsWith("/auth/login") ? json({ access_token: makeToken(), token_type: "bearer", expires_in_seconds: 600 }) : json([]);
    renderApp(<App />, handler, "/login");
    await fill(userEvent.setup());
    expect(await screen.findByRole("heading", { name: "Ask a question" })).toBeInTheDocument();
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });

  it("shows the server message and request id, and clears the password on failure", async () => {
    renderApp(<LoginPage />, () => err(401, "INVALID_CREDENTIALS", "Invalid tenant, email, or password."), "/login");
    await fill(userEvent.setup());
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid tenant, email, or password.");
    expect(screen.getByRole("alert")).toHaveTextContent("req-9");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("tells a throttled user how long to wait", async () => {
    renderApp(<LoginPage />, () => err(429, "RATE_LIMITED", "Too many attempts.", { "retry-after": "42" }), "/login");
    await fill(userEvent.setup());
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 42s.");
  });

  it("refuses an unreadable token", async () => {
    renderApp(<LoginPage />, () => json({ access_token: "garbage", token_type: "bearer", expires_in_seconds: 600 }), "/login");
    await fill(userEvent.setup());
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
    expect(sessionStorage.getItem("ekc.session")).toBeNull();
  });
});

describe("routing and roles", () => {
  it("sends anonymous users to the login page", () => {
    renderApp(<App />, () => json([]), "/documents");
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("hides admin navigation and blocks admin pages for ordinary users", () => {
    signInAs(["analyst"]);
    renderApp(<App />, () => json([]), "/usage");
    expect(screen.getByText("Your account does not have access to this page.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Usage" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Audit" })).toBeNull();
  });

  it("shows audit (not usage) to a security auditor", () => {
    signInAs(["security_auditor"]);
    renderApp(<App />, () => json([]), "/");
    expect(screen.getByRole("link", { name: "Audit" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Usage" })).toBeNull();
  });

  it("signs out when the API says the token is no longer valid", async () => {
    signInAs(["analyst"]);
    renderApp(
      <Routes>
        <Route path="*" element={<App />} />
      </Routes>,
      () => err(401, "INVALID_TOKEN", "The provided access token is invalid or has expired."),
      "/documents",
    );
    expect(await screen.findByText("Your session ended. Please sign in again.")).toBeInTheDocument();
  });
});

describe("query", () => {
  const answer = {
    status: "ANSWERED",
    message: "Refunds take 5 days <script>alert(1)</script>",
    route: { route: "document_qa", confidence: 0.91, classifier_version: "v1", reason: "policy question", fallback_used: false },
    citations: [{ source_id: "S1", document_id: "d1", document_title: "Refund policy", document_version: 3, section: "Timing", page: 2 }],
    analytics: { columns: ["day", "n"], rows: [["Mon", 3], ["Tue", { x: 1 }]], truncated: true },
    payload: { k: "v" },
    trace: {},
  };

  it("renders the answer as text with sources and data, never as HTML", async () => {
    signInAs();
    const seen = vi.fn();
    renderApp(<QueryPage />, (url, init) => { seen(url, init.body); return json(answer); });
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Your question"), "How long do refunds take?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/Refunds take 5 days <script>alert\(1\)<\/script>/)).toBeInTheDocument();
    expect(document.querySelector("article script")).toBeNull();
    expect(screen.getByText("Refund policy")).toBeInTheDocument();
    expect(screen.getByText(/version 3, page 2, Timing/)).toBeInTheDocument();
    expect(screen.getByText('{"x":1}')).toBeInTheDocument();
    expect(screen.getByText("Showing the first rows only.")).toBeInTheDocument();
    expect(JSON.parse(seen.mock.calls[0]?.[1] as string)).toEqual({ query: "How long do refunds take?" });
  });

  it("marks abstentions distinctly and disables Ask for blank input", async () => {
    signInAs();
    renderApp(<QueryPage />, () => json({ ...answer, status: "ABSTAINED", citations: [], analytics: null, payload: null }));
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Your question"), "x");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("abstained")).toHaveClass("warn");
  });

  it("offers a retry for transient failures only", async () => {
    signInAs();
    renderApp(<QueryPage />, () => err(503, "DEPENDENCY_UNAVAILABLE", "A dependency is unavailable.", { "retry-after": "2" }));
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Your question"), "x");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Try again in 2s.");
  });
});

describe("documents", () => {
  it("polls until processing finishes and then stops", async () => {
    signInAs();
    let calls = 0;
    renderApp(
      <DocumentsPage pollMs={10} />,
      () => { calls += 1; return json([{ id: "1", title: "a.txt", status: calls < 3 ? "embedding" : "active" }]); },
    );
    expect(await screen.findByText("Processing")).toBeInTheDocument();
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    const settled = calls;
    await new Promise((r) => setTimeout(r, 60));
    expect(calls).toBe(settled);
  });

  it("rejects an unsupported file before any network call", async () => {
    signInAs();
    const uploads = vi.fn();
    renderApp(<DocumentsPage />, (url, init) => { if (init.method === "POST") uploads(url); return json([]); });
    await screen.findByText("No documents yet.");
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    // `accept` is a UI hint only; fireEvent bypasses it exactly like a hostile user would
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(input as HTMLInputElement, { target: { files: [new File(["x"], "evil.exe")] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("Unsupported file type");
    expect(uploads).not.toHaveBeenCalled();
  });

  it("uploads a valid file and refreshes the list", async () => {
    signInAs();
    const list = [{ id: "1", title: "old.txt", status: "active" }];
    renderApp(<DocumentsPage />, (_u, init) => {
      if (init.method === "POST") {
        list.push({ id: "2", title: "new.txt", status: "active" });
        return json({ id: "2", title: "new.txt", status: "uploaded", content_hash: "h" }, { status: 202 });
      }
      return json(list);
    });
    await screen.findByText("old.txt");
    await userEvent.setup().upload(document.querySelector<HTMLInputElement>('input[type="file"]') as HTMLInputElement, new File(["hello"], "new.txt", { type: "text/plain" }));
    expect(await screen.findByText("new.txt")).toBeInTheDocument();
  });
});

describe("audit", () => {
  it("reports a broken chain as a security incident", async () => {
    signInAs(["security_auditor"]);
    renderApp(<AuditPage />, (url) =>
      url.includes("/verify") ? json({ ok: false, checked: 1, first_bad_seq: 2, reason: "row content does not match its hash" }) : json([]),
    );
    await userEvent.setup().click(await screen.findByRole("button", { name: "Verify integrity" }));
    await waitFor(() => { expect(screen.getByRole("alert")).toHaveTextContent("FAILED at event 2"); });
    expect(screen.getByRole("alert")).toHaveTextContent("security incident");
  });

  it("pages older events using the last sequence number", async () => {
    signInAs(["admin"]);
    const mk = (from: number, n: number) =>
      Array.from({ length: n }, (_, i) => ({ seq: from - i, occurred_at: "2026-01-01T00:00:00Z", actor_user_id: null, action: `a${String(from - i)}`, resource_type: "doc", resource_id: null, outcome: "success", request_id: null, details: {} }));
    const urls: string[] = [];
    renderApp(<AuditPage />, (url) => { urls.push(url); return json(url.includes("before_seq") ? mk(50, 3) : mk(100, 50)); });
    await userEvent.setup().click(await screen.findByRole("button", { name: "Load older events" }));
    await screen.findByText("a50");
    expect(urls.at(-1)).toContain("before_seq=51");
    expect(screen.queryByRole("button", { name: "Load older events" })).toBeNull();
  });
});
