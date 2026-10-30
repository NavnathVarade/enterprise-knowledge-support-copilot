import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiClient } from "../api/client";
import type { SignupInput } from "../api/types";
import { clearSession, decodeClaims, loadSession, saveSession } from "./session";
import type { Claims, Session } from "./session";

interface AuthValue {
  api: ApiClient;
  claims: Claims | null;
  signIn: (tenantSlug: string, email: string, password: string) => Promise<void>;
  signUp: (input: SignupInput) => Promise<void>;
  signOut: () => void;
  hasRole: (...roles: string[]) => boolean;
  expiredNotice: boolean;
}

const Ctx = createContext<AuthValue | null>(null);

export function AuthProvider({ children, fetchImpl }: { children: ReactNode; fetchImpl?: typeof fetch }) {
  const [session, setSession] = useState<Session | null>(() => loadSession());
  const [expiredNotice, setExpiredNotice] = useState(false);
  const sessionRef = useRef(session);
  sessionRef.current = session;

  const signOut = useCallback(() => {
    clearSession();
    setSession(null);
  }, []);

  const api = useMemo(
    () =>
      new ApiClient({
        getToken: () => sessionRef.current?.token ?? null,
        onUnauthorized: () => {
          setExpiredNotice(true);
          signOut();
        },
        ...(fetchImpl !== undefined ? { fetchImpl } : {}),
      }),
    [fetchImpl, signOut],
  );

  // Sign out the moment the token expires instead of waiting for a 401.
  useEffect(() => {
    if (session === null) return;
    const ms = Math.max(0, session.expiresAt - Date.now());
    const timer = setTimeout(() => {
      setExpiredNotice(true);
      signOut();
    }, Math.min(ms, 2 ** 31 - 1));
    return () => {
      clearTimeout(timer);
    };
  }, [session, signOut]);

  const signIn = useCallback(
    async (tenantSlug: string, email: string, password: string) => {
      const res = await api.login(tenantSlug, email, password);
      const next: Session = { token: res.access_token, expiresAt: Date.now() + res.expires_in_seconds * 1000 };
      if (decodeClaims(next.token) === null) throw new Error("The server returned an unreadable token.");
      saveSession(next);
      setExpiredNotice(false);
      setSession(next);
    },
    [api],
  );

  const signUp = useCallback(
    async (input: SignupInput) => {
      const res = await api.signup(input);
      const next: Session = { token: res.access_token, expiresAt: Date.now() + res.expires_in_seconds * 1000 };
      if (decodeClaims(next.token) === null) throw new Error("The server returned an unreadable token.");
      saveSession(next);
      setExpiredNotice(false);
      setSession(next);
    },
    [api],
  );

  const claims = useMemo(() => (session ? decodeClaims(session.token) : null), [session]);
  const hasRole = useCallback((...roles: string[]) => roles.some((r) => claims?.roles.includes(r)), [claims]);

  const value = useMemo(
    () => ({ api, claims, signIn, signUp, signOut, hasRole, expiredNotice }),
    [api, claims, signIn, signUp, signOut, hasRole, expiredNotice],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (v === null) throw new Error("useAuth must be used inside <AuthProvider>");
  return v;
}
