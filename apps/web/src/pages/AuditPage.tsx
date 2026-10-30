import { useCallback, useEffect, useState } from "react";
import type { AuditEvent, ChainVerification } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, PageHeader, Skeleton } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { formatTime } from "../lib/format";

const PAGE = 50;

export function AuditPage() {
  const { api } = useAuth();
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [verification, setVerification] = useState<ChainVerification | null>(null);
  const [verifyError, setVerifyError] = useState<unknown>(null);
  const [verifying, setVerifying] = useState(false);
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  const [filter, setFilter] = useState("");

  const load = useCallback(
    async (before?: number) => {
      setLoading(true);
      try {
        const rows = await api.auditEvents(PAGE, before);
        setEvents((prev) => (before === undefined ? rows : [...prev, ...rows]));
        setMore(rows.length === PAGE);
        setError(null);
      } catch (err) {
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [api],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const verify = () => {
    setVerifying(true);
    setVerification(null);
    setVerifyError(null);
    api
      .verifyAudit()
      .then(setVerification)
      .catch((err: unknown) => {
        setVerifyError(err);
      })
      .finally(() => {
        setVerifying(false);
      });
  };

  const last = events[events.length - 1];
  const needle = filter.trim().toLowerCase();
  const shown = needle === "" ? events : events.filter((e) => `${e.action} ${e.resource_type} ${e.outcome}`.toLowerCase().includes(needle));

  return (
    <>
      <PageHeader
        title="Audit trail"
        crumbs={[{ label: "Workspace", to: "/" }, { label: "Audit" }]}
        description="A tamper-evident record of sensitive actions in your organization, newest first."
        actions={
          <button type="button" onClick={verify} disabled={verifying}>
            <Icon name="shield" size={16} />
            {verifying ? "Verifying…" : "Verify integrity"}
          </button>
        }
      />
      {verification !== null &&
        (verification.ok ? (
          <p role="status" className="notice good">
            Chain intact: {verification.checked} events verified.
          </p>
        ) : (
          <div role="alert" className="error incident">
            <p>
              <strong>Integrity check FAILED at event {verification.first_bad_seq}</strong>: {verification.reason}. Treat as a security incident.
            </p>
            <p className="muted">{verification.checked} events were checked before the problem was found. Preserve the database and escalate to your security team.</p>
          </div>
        ))}
      {verifyError !== null && (
        <ErrorNote
          error={verifyError}
          onRetry={verify}
          title="The integrity check could not be completed. This says nothing about the chain itself."
        />
      )}
      {error !== null && <ErrorNote error={error} onRetry={() => void load()} />}

      <div className={`split${selected !== null ? " has-detail" : ""}`}>
        <Card title="Events">
          <div className="toolbar">
            <div className="field">
              <label htmlFor="audit-filter">Filter loaded events</label>
              <input
                id="audit-filter"
                value={filter}
                onChange={(e) => {
                  setFilter(e.target.value);
                }}
                placeholder="action, resource or outcome"
              />
            </div>
            <p className="muted">Filters only the {events.length} events loaded so far.</p>
          </div>
          {loading && events.length === 0 && <Skeleton lines={5} label="Loading audit events" />}
          {!loading && events.length === 0 && error === null && <EmptyState title="No audit events yet." icon="audit" />}
          {events.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">#</th>
                    <th scope="col">Time</th>
                    <th scope="col">Action</th>
                    <th scope="col">Resource</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">
                      <span className="sr-only">Inspect</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((ev) => (
                    <tr key={ev.seq}>
                      <td>{ev.seq}</td>
                      <td>{formatTime(ev.occurred_at)}</td>
                      <td>{ev.action}</td>
                      <td>
                        {ev.resource_type}
                        {ev.resource_id !== null ? ` ${ev.resource_id}` : ""}
                      </td>
                      <td>
                        <Badge tone={ev.outcome === "success" ? "good" : "bad"}>{ev.outcome}</Badge>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="link-btn"
                          aria-label={`Inspect event ${String(ev.seq)}`}
                          onClick={() => {
                            setSelected(ev);
                          }}
                        >
                          Inspect
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {loading && events.length > 0 && <p className="muted">Loading…</p>}
          {more && !loading && last !== undefined && (
            <p>
              <button type="button" className="secondary" onClick={() => void load(last.seq)}>
                Load older events
              </button>
            </p>
          )}
        </Card>
        {selected !== null && (
          <Card
            title={`Event ${String(selected.seq)}`}
            actions={
              <button
                type="button"
                className="icon-btn"
                aria-label="Close event details"
                onClick={() => {
                  setSelected(null);
                }}
              >
                <Icon name="close" />
              </button>
            }
          >
            <dl className="kv">
              <dt>Time</dt>
              <dd>{formatTime(selected.occurred_at)}</dd>
              <dt>Action</dt>
              <dd>{selected.action}</dd>
              <dt>Outcome</dt>
              <dd>{selected.outcome}</dd>
              <dt>Resource</dt>
              <dd>
                {selected.resource_type}
                {selected.resource_id !== null ? ` ${selected.resource_id}` : ""}
              </dd>
              <dt>Actor</dt>
              <dd>{selected.actor_user_id ?? "none (unauthenticated)"}</dd>
              <dt>Request ID</dt>
              <dd>{selected.request_id ?? "none"}</dd>
            </dl>
            <h3>Details</h3>
            <pre>{JSON.stringify(selected.details, null, 2)}</pre>
          </Card>
        )}
      </div>
    </>
  );
}
