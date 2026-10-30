import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../auth/AuthContext";
import { saveSession } from "../auth/session";
import { ToastProvider } from "../components/Toast";
import { ThemeProvider } from "../theme/ThemeContext";

export function makeToken(claims: Record<string, unknown> = {}): string {
  const enc = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const payload = {
    sub: "u1",
    email: "ada@example.com",
    tenant_id: "t1",
    roles: ["analyst"],
    exp: Math.floor(Date.now() / 1000) + 600,
    ...claims,
  };
  return `${enc({ alg: "none" })}.${enc(payload)}.sig`;
}

export function signInAs(roles: string[] = ["analyst"]): void {
  saveSession({ token: makeToken({ roles }), expiresAt: Date.now() + 600_000 });
}

export function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

export type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

export function renderApp(ui: ReactElement, handler: Handler, route = "/") {
  const fetchImpl = ((input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(handler(String(input), init ?? {}))) as typeof fetch;
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ThemeProvider>
        <ToastProvider>
          <AuthProvider fetchImpl={fetchImpl}>{ui}</AuthProvider>
        </ToastProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}
