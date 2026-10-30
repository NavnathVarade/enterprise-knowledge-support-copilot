import { useState } from "react";
import type { FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ErrorNote } from "../components/ErrorNote";
import { AuthShell } from "../components/AuthShell";
import { Field, PasswordInput } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { safeRedirect } from "../lib/redirect";

export function LoginPage() {
  const { signIn, claims, expiredNotice } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = safeRedirect((location.state as { from?: unknown } | null)?.from);
  const [tenant, setTenant] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (claims !== null) return <Navigate to={from} replace />;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    signIn(tenant.trim(), email.trim(), password)
      .then(() => {
        navigate(from, { replace: true });
      })
      .catch((err: unknown) => {
        setError(err);
        setPassword("");
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <AuthShell
      title="Sign in"
      subtitle="Use the organization identifier, email and password for your workspace."
      footer={
        <>
          New here? <Link to="/signup">Create an organization</Link>
        </>
      }
    >
      {expiredNotice && (
        <p role="status" className="notice warn">
          Your session ended. Please sign in again.
        </p>
      )}
      <form onSubmit={submit} noValidate={false}>
        <Field label="Organization" hint="The identifier chosen when the organization was created, e.g. acme.">
          {(p) => (
            <input
              {...p}
              value={tenant}
              onChange={(e) => {
                setTenant(e.target.value);
              }}
              autoComplete="organization"
              autoCapitalize="none"
              spellCheck={false}
              required
            />
          )}
        </Field>
        <Field label="Email">
          {(p) => (
            <input
              {...p}
              type="email"
              value={email}
              onChange={(e) => {
                setEmail(e.target.value);
              }}
              autoComplete="username"
              required
            />
          )}
        </Field>
        <Field label="Password">
          {(p) => (
            <PasswordInput
              {...p}
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
              }}
              autoComplete="current-password"
              required
            />
          )}
        </Field>
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {error !== null && <ErrorNote error={error} />}
    </AuthShell>
  );
}
