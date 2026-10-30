import { useCallback, useEffect, useState } from "react";
import type { ReadyReport } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { Icon } from "../components/Icon";
import { Badge, Card, PageHeader, Skeleton } from "../components/ui";
import type { Tone } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { formatTime } from "../lib/format";

interface Snapshot {
  live: boolean;
  httpStatus: number;
  report: ReadyReport;
  at: string;
}

function checkTone(value: string): Tone {
  if (value === "ok") return "good";
  if (value === "disabled") return "neutral";
  if (value.startsWith("degraded")) return "warn";
  return "bad";
}

function overallTone(status: string): Tone {
  if (status === "ready") return "good";
  if (status === "degraded") return "warn";
  return "bad";
}

/** Only what GET /health/live and /health/ready actually return. No latency, uptime or SLA claims. */
export function SystemPage() {
  const { api } = useAuth();
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const check = useCallback(async () => {
    setBusy(true);
    try {
      const live = await api.live();
      const ready = await api.ready();
      setSnap({ live: live.status === "alive", httpStatus: ready.httpStatus, report: ready.report, at: new Date().toISOString() });
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    void check();
  }, [check]);

  return (
    <>
      <PageHeader
        title="System status"
        crumbs={[{ label: "Workspace", to: "/" }, { label: "System status" }]}
        description="What the API reports about itself and its dependencies right now."
        actions={
          <button type="button" className="secondary" onClick={() => void check()} disabled={busy}>
            <Icon name="refresh" size={16} />
            {busy ? "Checking…" : "Check again"}
          </button>
        }
      />
      {error !== null && <ErrorNote error={error} onRetry={() => void check()} />}
      {snap === null && error === null && <Skeleton lines={4} label="Checking system status" />}
      {snap !== null && (
        <>
          <div className="stats">
            <div className="stat">
              <div className="stat-label">API process</div>
              <div className="stat-value">{snap.live ? "Running" : "Unknown"}</div>
              <p className="muted">From the liveness probe.</p>
            </div>
            <div className="stat">
              <div className="stat-label">Readiness</div>
              <div className="stat-value">
                <Badge tone={overallTone(snap.report.status)}>{snap.report.status.replace(/_/g, " ")}</Badge>
              </div>
              <p className="muted">HTTP {snap.httpStatus}. Checked {formatTime(snap.at)}.</p>
            </div>
          </div>
          <Card title="Dependencies">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Dependency</th>
                    <th scope="col">Reported state</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(snap.report.checks).map(([name, value]) => (
                    <tr key={name}>
                      <td>{name}</td>
                      <td>
                        <Badge tone={checkTone(value)}>{value}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted">
              “disabled” means the dependency is switched off in this environment (for example Kafka in development). “degraded” means the API still works
              but a protection or optimisation layer is impaired. This page reports a single point-in-time probe, not historical availability or performance.
            </p>
          </Card>
        </>
      )}
    </>
  );
}
