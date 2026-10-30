/** Mirrors the server's signup password policy (apps/api/app/routes/auth.py). Advisory only: the
 * server is the authority and its message is shown if it disagrees. */
export const MIN_PASSWORD_LENGTH = 10;
const MAX_BYTES = 72;

export function passwordProblem(password: string, email: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH) return `Use at least ${String(MIN_PASSWORD_LENGTH)} characters.`;
  if (new TextEncoder().encode(password).length > MAX_BYTES) return `Use at most ${String(MAX_BYTES)} bytes.`;
  if (!/\p{L}/u.test(password) || !/\p{Nd}/u.test(password)) return "Include at least one letter and one digit.";
  const local = (email.split("@")[0] ?? "").toLowerCase();
  if (local.length >= 4 && password.toLowerCase().includes(local)) return "Do not include the part of your email before the @.";
  return null;
}

export const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$/;

export function slugProblem(slug: string): string | null {
  return SLUG_PATTERN.test(slug) ? null : "Use 3–63 lowercase letters, digits or hyphens; no hyphen at the start or end.";
}

/** Suggest an identifier from an organization name (the user can edit it). */
export function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63)
    .replace(/-+$/g, "");
}
