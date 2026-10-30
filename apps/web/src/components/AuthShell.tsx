import type { ReactNode } from "react";
import { Icon } from "./Icon";

/** Shared frame for the signed-out pages (login, signup). */
export function AuthShell({ title, subtitle, children, footer }: { title: string; subtitle: string; children: ReactNode; footer: ReactNode }) {
  return (
    <div className="auth">
      <aside className="auth-side">
        <div className="auth-brand">
          <span className="brand-mark" aria-hidden="true">
            <Icon name="ask" size={18} />
          </span>
          <strong>Knowledge Copilot</strong>
        </div>
        <h2 className="auth-pitch">Answers from your own documents, with the sources to check them.</h2>
        <ul className="auth-points">
          <li>
            <Icon name="shield" size={18} />
            <span>Every answer respects who is allowed to see which document.</span>
          </li>
          <li>
            <Icon name="documents" size={18} />
            <span>Citations name the document, version and page they came from.</span>
          </li>
          <li>
            <Icon name="audit" size={18} />
            <span>Sensitive actions are recorded in a tamper-evident audit trail.</span>
          </li>
        </ul>
      </aside>
      <main className="auth-main" id="main">
        <div className="auth-card">
          <h1>{title}</h1>
          <p className="lede">{subtitle}</p>
          {children}
          <p className="auth-foot">{footer}</p>
        </div>
      </main>
    </div>
  );
}
