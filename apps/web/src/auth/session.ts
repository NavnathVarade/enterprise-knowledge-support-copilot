// Access-token handling. The token lives in sessionStorage (cleared when the tab closes, never
// shared across tabs). XSS could read it: the defences are the strict CSP set by nginx, no raw
// HTML rendering (lint-enforced) and a short server-side TTL. There are no refresh tokens, so
// when it expires the user signs in again.

const KEY = "ekc.session";

export interface Session {
  token: string;
  expiresAt: number; // epoch ms
}

export interface Claims {
  sub: string;
  email: string;
  tenantId: string;
  roles: string[];
  exp: number; // epoch seconds
}

function b64urlDecode(s: string): string {
  const padded = s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=");
  const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/** Reads claims for DISPLAY and menu gating only. This is not verification: the server
 *  re-checks signature, expiry and roles on every request. */
export function decodeClaims(token: string): Claims | null {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[1] === undefined) return null;
  try {
    const raw: unknown = JSON.parse(b64urlDecode(parts[1]));
    if (typeof raw !== "object" || raw === null) return null;
    const c = raw as Record<string, unknown>;
    const roles = Array.isArray(c["roles"]) ? c["roles"].filter((r): r is string => typeof r === "string") : [];
    if (typeof c["sub"] !== "string" || typeof c["exp"] !== "number") return null;
    return {
      sub: c["sub"],
      email: typeof c["email"] === "string" ? c["email"] : "",
      tenantId: typeof c["tenant_id"] === "string" ? c["tenant_id"] : "",
      roles,
      exp: c["exp"],
    };
  } catch {
    return null;
  }
}

export function isExpired(session: Session, now: number = Date.now()): boolean {
  return now >= session.expiresAt;
}

export function saveSession(session: Session): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    // storage blocked (private mode): the session simply lives in memory for this page load
  }
}

export function loadSession(now: number = Date.now()): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw === null) return null;
    const v: unknown = JSON.parse(raw);
    if (typeof v !== "object" || v === null) return null;
    const s = v as Record<string, unknown>;
    if (typeof s["token"] !== "string" || typeof s["expiresAt"] !== "number") return null;
    const session = { token: s["token"], expiresAt: s["expiresAt"] };
    if (isExpired(session, now) || decodeClaims(session.token) === null) {
      clearSession();
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // nothing to clear
  }
}
