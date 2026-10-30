import { useEffect, useId, useRef, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { Link } from "react-router-dom";
import { Icon } from "./Icon";

export type Tone = "neutral" | "good" | "warn" | "bad" | "info";

/** Status is always conveyed by the text, never by colour alone. */
export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge ${tone === "warn" ? "warn" : ""} tone-${tone}`}>{children}</span>;
}

export function PageHeader({
  title,
  crumbs,
  description,
  actions,
}: {
  title: string;
  crumbs?: { label: string; to?: string }[];
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {crumbs !== undefined && (
          <nav aria-label="Breadcrumb" className="crumbs">
            <ol>
              {crumbs.map((c, i) => (
                <li key={c.label} aria-current={i === crumbs.length - 1 ? "page" : undefined}>
                  {c.to !== undefined && i < crumbs.length - 1 ? <Link to={c.to}>{c.label}</Link> : c.label}
                </li>
              ))}
            </ol>
          </nav>
        )}
        <h1>{title}</h1>
        {description !== undefined && <p className="lede">{description}</p>}
      </div>
      {actions !== undefined && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card">
      {(title !== undefined || actions !== undefined) && (
        <div className="card-head">
          {title !== undefined && <h2>{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Skeleton({ lines = 3, label = "Loading" }: { lines?: number; label?: string }) {
  return (
    <div className="skeleton-group" role="status" aria-label={label}>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton" />
      ))}
    </div>
  );
}

export function EmptyState({ title, children, icon = "info" }: { title: string; children?: ReactNode; icon?: ComponentProps<typeof Icon>["name"] }) {
  return (
    <div className="empty">
      <Icon name={icon} size={28} />
      <p className="empty-title">{title}</p>
      {children !== undefined && <p className="muted">{children}</p>}
    </div>
  );
}

/** Label + optional hint + optional error wired up with aria-describedby / aria-invalid. */
export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: true }) => ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  const described = [hint !== undefined ? hintId : null, error ? errId : null].filter((x): x is string => x !== null).join(" ");
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children({
        id,
        ...(described !== "" ? { "aria-describedby": described } : {}),
        ...(error ? { "aria-invalid": true as const } : {}),
      })}
      {hint !== undefined && (
        <p id={hintId} className="hint">
          {hint}
        </p>
      )}
      {error ? (
        <p id={errId} className="field-error">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function PasswordInput(props: Omit<ComponentProps<"input">, "type">) {
  const [shown, setShown] = useState(false);
  return (
    <div className="password">
      <input {...props} type={shown ? "text" : "password"} />
      <button
        type="button"
        className="icon-btn"
        aria-label={shown ? "Hide password" : "Show password"}
        aria-pressed={shown}
        onClick={() => {
          setShown((v) => !v);
        }}
      >
        <Icon name={shown ? "eyeOff" : "eye"} />
      </button>
    </div>
  );
}

/** Modal confirmation. A plain element rather than <dialog> so it behaves the same everywhere. */
export function ConfirmDialog({
  title,
  confirmLabel,
  onConfirm,
  onCancel,
  children,
}: {
  title: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("button.secondary")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
      if (e.key === "Tab" && ref.current !== null) {
        const f = ref.current.querySelectorAll<HTMLElement>("button");
        const first = f[0];
        const last = f[f.length - 1];
        if (first === undefined || last === undefined) return;
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [onCancel]);
  return (
    <div className="scrim">
      <div ref={ref} className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h2 id={titleId}>{title}</h2>
        <div className="dialog-body">{children}</div>
        <div className="dialog-actions">
          <button type="button" className="secondary" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
