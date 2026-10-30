import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { json, makeToken, renderApp, signInAs } from "../test/helpers";
import type { Handler } from "../test/helpers";
import { UsagePage, usd } from "./UsagePage";
import { AuditPage } from "./AuditPage";
import { SystemPage } from "./SystemPage";
import { QueryPage } from "./QueryPage";

const err = (status: number, code: string, message: string, headers: Record<string, string> = {}) =>
  json({ error: { code, message, request_id: "req-7" } }, { status, headers });

const session = (extra: Record<string, unknown> = {}) =>
  json({ access_token: makeToken({ roles: ["admin"] }), token_type: "bearer", expires_in_seconds: 600, ...extra });

describe("signup", () => {
  const fillValid = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.type(screen.getByLabelText("Organization name"), "Acme Inc");
    await user.type(screen.getByLabelText("Your name"), "Ada Lovelace");
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "correct-horse-battery-9");
    await user.type(screen.getByLabelText("Confirm password"), "correct-horse-battery-9");
  };

  it("suggests an identifier, creates the organization and lands signed in", async () => {
    const bodies: unknown[] = [];
    const handler: Handler = (url, init) => {
      if (url.endsWith("/auth/signup")) {
        bodies.push(JSON.parse(init.body as string));
        return session({ tenant_slug: "acme-inc" });
      }
      return json([]);
    };
    renderApp(<App />, handler, "/signup");
    const user = userEvent.setup();
    await fillValid(user);
    expect(screen.getByLabelText("Organization identifier")).toHaveValue("acme-inc");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByRole("heading", { name: "Ask a question" })).toBeInTheDocument();
    expect(bodies).toEqual([
      { organization_name: "Acme Inc", tenant_slug: "acme-inc", display_name: "Ada Lovelace", email: "ada@example.com", password: "correct-horse-battery-9" },
    ]);
    expect(screen.getByText(/Sign in next time with the identifier "acme-inc"/)).toBeInTheDocument();
    expect(JSON.stringify(sessionStorage)).not.toContain("correct-horse");
  });

  it("validates in the browser without calling the API", async () => {
    const calls = vi.fn();
    renderApp(<App />, (u) => { calls(u); return json([]); }, "/signup");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Organization name"), "Acme");
    await user.type(screen.getByLabelText("Your name"), "Ada");
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.type(screen.getByLabelText("Confirm password"), "short");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByText("Use at least 10 characters.")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
    expect(calls).not.toHaveBeenCalled();
  });

  it("flags mismatched confirmation", async () => {
    renderApp(<App />, () => json([]), "/signup");
    const user = userEvent.setup();
    await fillValid(user);
    await user.clear(screen.getByLabelText("Confirm password"));
    await user.type(screen.getByLabelText("Confirm password"), "different-pass-1");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByText("The passwords do not match.")).toBeInTheDocument();
  });

  it("shows a taken identifier on its own field and clears the passwords", async () => {
    renderApp(<App />, () => err(409, "TENANT_SLUG_TAKEN", "That organization identifier is already in use. Choose another."), "/signup");
    const user = userEvent.setup();
    await fillValid(user);
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByText(/already in use/)).toBeInTheDocument();
    expect(screen.getByLabelText("Organization identifier")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("explains a deployment where signup is switched off, with the request id", async () => {
    renderApp(<App />, () => err(403, "SIGNUP_DISABLED", "Self-service sign-up is not enabled on this deployment."), "/signup");
    const user = userEvent.setup();
    await fillValid(user);
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("not enabled on this deployment");
    expect(alert).toHaveTextContent("req-7");
  });

  it("tells a throttled visitor how long to wait", async () => {
    renderApp(<App />, () => err(429, "SIGNUP_THROTTLED", "Too many sign-up attempts from this address. Try again later.", { "retry-after": "120" }), "/signup");
    const user = userEvent.setup();
    await fillValid(user);
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 120s.");
  });

  it("reveals and hides the password on request", async () => {
    renderApp(<App />, () => json([]), "/signup");
    const user = userEvent.setup();
    const pw = screen.getByLabelText("Password");
    expect(pw).toHaveAttribute("type", "password");
    await user.click(screen.getAllByRole("button", { name: "Show password" })[0] as HTMLElement);
    expect(pw).toHaveAttribute("type", "text");
  });

  it("links between sign in and sign up", async () => {
    renderApp(<App />, () => json([]), "/login");
    await userEvent.setup().click(screen.getByRole("link", { name: "Create an organization" }));
    expect(screen.getByRole("heading", { name: "Create your organization" })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("link", { name: "Sign in" }));
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("login redirect safety", () => {
  it("ignores a hostile return path and lands on the home page", async () => {
    const { MemoryRouter } = await import("react-router-dom");
    const { render } = await import("@testing-library/react");
    const { AuthProvider } = await import("../auth/AuthContext");
    const { ToastProvider } = await import("../components/Toast");
    const { ThemeProvider } = await import("../theme/ThemeContext");
    const fetchImpl = ((u: RequestInfo | URL) => Promise.resolve(String(u).endsWith("/auth/login") ? session() : json([]))) as typeof fetch;
    render(
      <MemoryRouter initialEntries={[{ pathname: "/login", state: { from: "https://evil.example/phish" } }]}>
        <ThemeProvider><ToastProvider><AuthProvider fetchImpl={fetchImpl}><App /></AuthProvider></ToastProvider></ThemeProvider>
      </MemoryRouter>,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Organization"), "acme");
    await user.type(screen.getByLabelText("Email"), "a@b.co");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Ask a question" })).toBeInTheDocument();
  });
});

describe("app shell", () => {
  it("offers a mobile menu that opens, closes with Escape and exposes one main navigation", async () => {
    signInAs(["analyst"]);
    renderApp(<App />, () => json([]), "/");
    const user = userEvent.setup();
    const open = screen.getByRole("button", { name: "Open navigation menu" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await user.click(open);
    expect(open).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("navigation", { name: "Main" })).toHaveLength(1);
    await user.keyboard("{Escape}");
    expect(open).toHaveAttribute("aria-expanded", "false");
  });

  it("collapses the sidebar and remembers it for the next visit", async () => {
    signInAs(["analyst"]);
    renderApp(<App />, () => json([]), "/");
    await userEvent.setup().click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(document.querySelector(".shell")).toHaveClass("collapsed");
    expect(screen.getByRole("link", { name: "Documents" })).toBeInTheDocument(); // still named when icon-only
    expect(localStorage.getItem("ekc.nav.collapsed")).toBe("1");
    localStorage.removeItem("ekc.nav.collapsed");
  });

  it("switches theme and persists only the preference", async () => {
    signInAs(["analyst"]);
    renderApp(<App />, () => json([]), "/");
    await userEvent.setup().click(screen.getByRole("button", { name: "Dark theme" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(localStorage.getItem("ekc.theme")).toBe("dark");
    await userEvent.setup().click(screen.getByRole("button", { name: "System theme" }));
    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(localStorage.getItem("ekc.theme")).toBeNull();
  });

  it("shows the system page to every signed-in role and keeps tokens out of local storage", () => {
    signInAs(["analyst"]);
    renderApp(<App />, () => json({ status: "alive", checks: {} }), "/system");
    expect(screen.getByRole("link", { name: "System status" })).toBeInTheDocument();
    expect(JSON.stringify({ ...localStorage })).not.toContain("eyJ");
  });
});

describe("system status page", () => {
  it("reports exactly what the API returned, including a degraded dependency", async () => {
    signInAs();
    renderApp(<SystemPage />, (url) =>
      url.endsWith("/health/live")
        ? json({ status: "alive" })
        : json({ status: "degraded", checks: { database: "ok", redis: "degraded: ConnectionError", kafka: "disabled" } }),
    );
    expect(await screen.findByText("degraded: ConnectionError")).toBeInTheDocument();
    expect(screen.getByText("disabled")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("shows not-ready from a 503 report and a failure when the API cannot be reached", async () => {
    signInAs();
    renderApp(<SystemPage />, (url) =>
      url.endsWith("/health/live")
        ? json({ status: "alive" })
        : json({ status: "not_ready", checks: { database: "unavailable: TimeoutError" } }, { status: 503 }),
    );
    expect(await screen.findByText("unavailable: TimeoutError")).toBeInTheDocument();
    expect(screen.getByText("not ready")).toBeInTheDocument();
  });

  it("surfaces an unreachable API as an error", async () => {
    signInAs();
    renderApp(<SystemPage />, () => { throw new TypeError("network"); });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server");
  });
});

describe("usage and budget", () => {
  const budget = { daily_token_limit: 1000, daily_cost_usd_limit: "2.5000", source: "tenant", used_llm_tokens_today: 250, known_cost_usd_today: "0.1200" };
  const summary = {
    since: "2026-01-01",
    daily: [{ day: "2026-01-02", kind: "llm", events: 2, input_tokens: 10, output_tokens: 5, known_cost_usd: "0.0000", unpriced_events: 2, unreported_token_events: 0 }],
    query_cost: { queries_counted: 0, queries_excluded_unpriced: 2, average_cost_usd: null },
    top_documents: [],
    note: "Costs exclude unpriced events.",
  };
  const handler = (put?: (body: unknown) => void): Handler => (url, init) => {
    if (url.includes("/usage/summary")) return json(summary);
    if (init.method === "PUT") {
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      put?.(body);
      return json({ ...budget, daily_token_limit: body["daily_token_limit"], daily_cost_usd_limit: body["daily_cost_usd_limit"] === null ? null : "9.0000" });
    }
    return json(budget);
  };

  it("never shows an unknown average cost as $0", async () => {
    signInAs(["admin"]);
    renderApp(<UsagePage />, handler());
    expect(await screen.findByText(/Average cost per priced query: not available/)).toBeInTheDocument();
    expect(usd(null)).toBe("not available");
    expect(screen.getByText(/2 excluded as unpriced/)).toBeInTheDocument();
  });

  it("shows server-confirmed values after saving", async () => {
    signInAs(["admin"]);
    renderApp(<UsagePage />, handler());
    const cost = await screen.findByLabelText("Cost per day in USD (empty = unlimited)");
    await waitFor(() => { expect(cost).toHaveValue("2.5000"); });
    await userEvent.setup().clear(cost);
    await userEvent.setup().type(cost, "9");
    await userEvent.setup().click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByText("Limits saved.")).toBeInTheDocument();
    expect(cost).toHaveValue("9.0000"); // what the server stored, not what was typed
  });

  it("asks before removing every limit, and sends nothing if cancelled", async () => {
    signInAs(["admin"]);
    const put = vi.fn();
    renderApp(<UsagePage />, handler(put));
    const tokens = await screen.findByLabelText("Tokens per day (empty = unlimited)");
    await waitFor(() => { expect(tokens).toHaveValue("1000"); });
    const user = userEvent.setup();
    await user.clear(tokens);
    await user.clear(screen.getByLabelText("Cost per day in USD (empty = unlimited)"));
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove all spending limits?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(put).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove limits" }));
    await waitFor(() => { expect(put).toHaveBeenCalledWith({ daily_token_limit: null, daily_cost_usd_limit: null }); });
  });
});

describe("audit", () => {
  const ev = { seq: 3, occurred_at: "2026-01-01T00:00:00Z", actor_user_id: "u-1", action: "auth.login", resource_type: "session", resource_id: null, outcome: "success", request_id: "rq-1", details: { reason: "<b>x</b>" } };

  it("inspects an event and renders its details as text", async () => {
    signInAs(["security_auditor"]);
    renderApp(<AuditPage />, () => json([ev]));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Inspect event 3" }));
    expect(screen.getByRole("heading", { name: "Event 3" })).toBeInTheDocument();
    expect(screen.getByText(/<b>x<\/b>/)).toBeInTheDocument();
    expect(document.querySelector("pre b")).toBeNull();
    expect(screen.getByText("rq-1")).toBeInTheDocument();
  });

  it("distinguishes a failed verification request from a failed chain", async () => {
    signInAs(["security_auditor"]);
    renderApp(<AuditPage />, (url) => (url.includes("/verify") ? err(503, "SERVICE_BUSY", "The service is busy.") : json([ev])));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Verify integrity" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("could not be completed");
    expect(alert).not.toHaveTextContent("security incident");
  });

  it("confirms an intact chain", async () => {
    signInAs(["security_auditor"]);
    renderApp(<AuditPage />, (url) => (url.includes("/verify") ? json({ ok: true, checked: 12, first_bad_seq: null, reason: null }) : json([ev])));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Verify integrity" }));
    expect(await screen.findByText("Chain intact: 12 events verified.")).toBeInTheDocument();
  });

  it("filters only the loaded events", async () => {
    signInAs(["security_auditor"]);
    renderApp(<AuditPage />, () => json([ev, { ...ev, seq: 2, action: "document.upload" }]));
    await screen.findByText("document.upload");
    await userEvent.setup().type(screen.getByLabelText("Filter loaded events"), "upload");
    expect(screen.queryByText("auth.login")).toBeNull();
  });
});

describe("ask", () => {
  it("cancels an in-flight question and shows no error", async () => {
    signInAs();
    renderApp(<QueryPage />, (_u, init) =>
      new Promise<Response>((_res, rej) => {
        (init.signal as AbortSignal).addEventListener("abort", () => { rej(new DOMException("a", "AbortError")); });
      }) as unknown as Response,
    );
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Your question"), "slow one");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled();
  });

  it("sends with Ctrl+Enter and warns near the limit", async () => {
    signInAs();
    const seen = vi.fn();
    renderApp(<QueryPage />, (_u, init) => { seen(init.body); return json({ status: "ABSTAINED", message: "m", route: { route: "r", confidence: 0.5, classifier_version: "v", reason: "x", fallback_used: true }, citations: [], analytics: null, payload: null, trace: {} }); });
    const box = screen.getByLabelText("Your question");
    fireEvent.change(box, { target: { value: "a".repeat(1850) } });
    expect(screen.getByText("1850/2000")).toHaveClass("near");
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    expect(await screen.findByText("abstained")).toBeInTheDocument();
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("keeps routing details behind an expandable section", async () => {
    signInAs();
    renderApp(<QueryPage />, () => json({ status: "ANSWERED", message: "m", route: { route: "document_qa", confidence: 0.9, classifier_version: "v9", reason: "why", fallback_used: false }, citations: [], analytics: null, payload: null, trace: {} }));
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Your question"), "q");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    const summary = await screen.findByText("How this was routed");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("v9")).toBeInTheDocument();
  });
});

describe("accessibility (axe, structural rules; contrast is not computed in jsdom)", () => {
  const rules = { rules: { "color-contrast": { enabled: false } } };
  const violations = async (el: Element) => (await axe.run(el, rules)).violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join("; ")}`);

  it.each(["/login", "/signup"])("%s has no violations", async (route) => {
    renderApp(<App />, () => json([]), route);
    expect(await violations(document.body)).toEqual([]);
  });

  it.each(["/", "/documents", "/system"])("%s has no violations when signed in", async (route) => {
    signInAs(["analyst"]);
    renderApp(<App />, (url) => (url.includes("health") ? json({ status: "ready", checks: { database: "ok" } }) : json([])), route);
    await screen.findByRole("navigation", { name: "Main" });
    expect(await violations(document.body)).toEqual([]);
  });

  it("audit and usage have no violations for an admin", async () => {
    signInAs(["admin"]);
    renderApp(<App />, (url) =>
      url.includes("/usage/summary")
        ? json({ since: "x", daily: [], query_cost: { queries_counted: 0, queries_excluded_unpriced: 0, average_cost_usd: null }, top_documents: [], note: "n" })
        : url.includes("/usage/budget")
          ? json({ daily_token_limit: null, daily_cost_usd_limit: null, source: "default", used_llm_tokens_today: 0, known_cost_usd_today: "0" })
          : json([]),
    "/usage");
    await screen.findByText("No usage recorded in this period.");
    expect(await violations(document.body)).toEqual([]);
  });
});
