import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { ApiError } from "../api/client";
import type { QueryResponse } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, PageHeader, Skeleton } from "../components/ui";
import type { Tone } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { formatCell } from "../lib/format";

const MAX = 2000;
const NEAR = 1800;

export function QueryPage() {
  const { api } = useAuth();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const controller = useRef<AbortController | null>(null);

  // Leaving the page cancels an in-flight question instead of letting it run to a dead screen.
  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  const ask = () => {
    controller.current?.abort();
    const c = new AbortController();
    controller.current = c;
    setBusy(true);
    setError(null);
    api
      .query(text.trim(), c.signal)
      .then((r) => {
        if (!c.signal.aborted) setResult(r);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.code === "CANCELLED") return;
        setResult(null);
        setError(err);
      })
      .finally(() => {
        if (controller.current === c) setBusy(false);
      });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim() !== "" && !busy) ask();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim() !== "" && !busy) {
      e.preventDefault();
      ask();
    }
  };
  const cancel = () => {
    controller.current?.abort();
    setBusy(false);
  };

  return (
    <>
      <PageHeader
        title="Ask a question"
        crumbs={[{ label: "Workspace", to: "/" }, { label: "Ask" }]}
        description="Answers are drawn only from documents you are allowed to see, and cite where they came from."
      />
      <Card>
        <form onSubmit={submit} className="composer">
          <label htmlFor="q">Your question</label>
          <textarea
            id="q"
            value={text}
            onChange={(e) => {
              setText(e.target.value);
            }}
            onKeyDown={onKey}
            maxLength={MAX}
            rows={4}
            aria-describedby="q-count q-help"
          />
          <div className="composer-foot">
            <p id="q-count" className={`counter muted${text.length >= NEAR ? " near" : ""}`}>
              {text.length}/{MAX}
            </p>
            <p id="q-help" className="hint">
              Ctrl or ⌘ + Enter to send
            </p>
            <div className="form-actions">
              {busy && (
                <button type="button" className="secondary" onClick={cancel}>
                  Cancel
                </button>
              )}
              <button type="submit" disabled={busy || text.trim() === ""}>
                <Icon name="send" size={16} />
                {busy ? "Thinking…" : "Ask"}
              </button>
            </div>
          </div>
        </form>
      </Card>
      {busy && <Skeleton lines={4} label="Waiting for an answer" />}
      {error !== null && <ErrorNote error={error} onRetry={ask} />}
      {result !== null && !busy && <Answer result={result} />}
      {result === null && error === null && !busy && (
        <EmptyState title="No question asked yet" icon="ask">
          Try asking about a policy, a procedure or a figure in one of your documents.
        </EmptyState>
      )}
    </>
  );
}

function statusTone(status: string): Tone {
  if (status === "ANSWERED") return "good";
  if (status === "ABSTAINED" || status === "NOT_SUPPORTED") return "warn";
  return "neutral";
}

/** Everything below is rendered as text: model output and document content are untrusted. */
function Answer({ result }: { result: QueryResponse }) {
  const { route } = result;
  return (
    <article aria-live="polite" className="answer card">
      <div className="answer-head">
        <Badge tone={statusTone(result.status)}>{result.status.replace(/_/g, " ").toLowerCase()}</Badge>
      </div>
      <p className="message">{result.message}</p>

      {result.citations.length > 0 && (
        <>
          <h2>Sources</h2>
          <ol className="sources">
            {result.citations.map((c) => (
              <li key={c.source_id}>
                <strong>{c.document_title}</strong>
                <div className="source-meta">
                  <span>
                    {`(version ${String(c.document_version)}${c.page !== null ? `, page ${String(c.page)}` : ""}${c.section !== null ? `, ${c.section}` : ""})`}
                  </span>
                </div>
              </li>
            ))}
          </ol>
        </>
      )}

      {result.analytics !== null && (
        <>
          <h2>Data</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {result.analytics.columns.map((c) => (
                    <th key={c} scope="col">
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.analytics.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j}>{formatCell(cell)}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {result.analytics.truncated && <p className="muted">Showing the first rows only.</p>}
        </>
      )}

      <h2>Technical details</h2>
      <details>
        <summary>How this was routed</summary>
        <dl className="kv">
          <dt>Route</dt>
          <dd>{route.route}</dd>
          <dt>Confidence</dt>
          <dd>{Math.round(route.confidence * 100)}%</dd>
          <dt>Reason</dt>
          <dd>{route.reason}</dd>
          <dt>Classifier</dt>
          <dd>{route.classifier_version}</dd>
          <dt>Fallback used</dt>
          <dd>{route.fallback_used ? "yes" : "no"}</dd>
        </dl>
      </details>
      {result.payload !== null && (
        <details>
          <summary>Details</summary>
          <pre>{JSON.stringify(result.payload, null, 2)}</pre>
        </details>
      )}
    </article>
  );
}
