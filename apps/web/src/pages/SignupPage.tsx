import { useState } from "react";
import type { FormEvent } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { ErrorNote } from "../components/ErrorNote";
import { AuthShell } from "../components/AuthShell";
import { useToast } from "../components/Toast";
import { Field, PasswordInput } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { MIN_PASSWORD_LENGTH, passwordProblem, slugProblem, suggestSlug } from "../lib/password";

type Errors = Partial<Record<"organization" | "slug" | "name" | "email" | "password" | "confirm", string>>;

export function SignupPage() {
  const { signUp, claims } = useAuth();
  const { notify } = useToast();
  const navigate = useNavigate();
  const [organization, setOrganization] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);

  if (claims !== null && !busy) return <Navigate to="/" replace />;

  const validate = (): Errors => {
    const e: Errors = {};
    if (organization.trim().length < 2) e.organization = "Enter your organization's name (at least 2 characters).";
    const sp = slugProblem(slug.trim().toLowerCase());
    if (sp !== null) e.slug = sp;
    if (name.trim() === "") e.name = "Enter your name.";
    if (!/^\S+@\S+\.\S+$/.test(email.trim())) e.email = "Enter a valid email address.";
    const pp = passwordProblem(password, email.trim());
    if (pp !== null) e.password = pp;
    else if (confirm !== password) e.confirm = "The passwords do not match.";
    return e;
  };

  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    if (busy) return;
    const found = validate();
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length > 0) return;
    setBusy(true);
    const finalSlug = slug.trim().toLowerCase();
    signUp({
      organization_name: organization.trim(),
      tenant_slug: finalSlug,
      display_name: name.trim(),
      email: email.trim(),
      password,
    })
      .then(() => {
        notify("success", `Organization created. Sign in next time with the identifier "${finalSlug}".`);
        navigate("/", { replace: true });
      })
      .catch((err: unknown) => {
        // Server answers are shown on the field they concern; anything else as a general alert.
        if (err instanceof ApiError && err.code === "TENANT_SLUG_TAKEN") setErrors({ slug: err.message });
        else if (err instanceof ApiError && err.code === "WEAK_PASSWORD") setErrors({ password: err.message });
        else setFailure(err);
        setPassword("");
        setConfirm("");
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <AuthShell
      title="Create your organization"
      subtitle="You become its administrator and can then upload documents and ask questions."
      footer={
        <>
          Already have an account? <Link to="/login">Sign in</Link>
        </>
      }
    >
      <form onSubmit={submit} noValidate>
        <Field label="Organization name" error={errors.organization ?? null}>
          {(p) => (
            <input
              {...p}
              value={organization}
              onChange={(e) => {
                setOrganization(e.target.value);
                if (!slugEdited) setSlug(suggestSlug(e.target.value));
              }}
              autoComplete="organization"
              maxLength={120}
              required
            />
          )}
        </Field>
        <Field
          label="Organization identifier"
          hint="Used when signing in. Lowercase letters, digits and hyphens."
          error={errors.slug ?? null}
        >
          {(p) => (
            <input
              {...p}
              value={slug}
              onChange={(e) => {
                setSlugEdited(true);
                setSlug(e.target.value);
              }}
              autoCapitalize="none"
              spellCheck={false}
              maxLength={63}
              required
            />
          )}
        </Field>
        <Field label="Your name" error={errors.name ?? null}>
          {(p) => (
            <input
              {...p}
              value={name}
              onChange={(e) => {
                setName(e.target.value);
              }}
              autoComplete="name"
              maxLength={120}
              required
            />
          )}
        </Field>
        <Field label="Email" error={errors.email ?? null}>
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
        <Field
          label="Password"
          hint={`At least ${String(MIN_PASSWORD_LENGTH)} characters with a letter and a digit.`}
          error={errors.password ?? null}
        >
          {(p) => (
            <PasswordInput
              {...p}
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
              }}
              autoComplete="new-password"
              required
            />
          )}
        </Field>
        <Field label="Confirm password" error={errors.confirm ?? null}>
          {(p) => (
            <PasswordInput
              {...p}
              value={confirm}
              onChange={(e) => {
                setConfirm(e.target.value);
              }}
              autoComplete="new-password"
              required
            />
          )}
        </Field>
        <button type="submit" disabled={busy}>
          {busy ? "Creating…" : "Create organization"}
        </button>
      </form>
      {failure !== null && <ErrorNote error={failure} />}
    </AuthShell>
  );
}
