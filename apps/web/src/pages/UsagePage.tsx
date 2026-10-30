import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { Budget, UsageSummary } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { Card, ConfirmDialog, PageHeader, Skeleton } from "../components/ui";
import { useAuth } from "../auth/AuthContext";

function parseLimit(raw: string): number | null | "invalid" {
  const t = raw.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) && n > 0 ? n : "invalid";
}

const nf = new Intl.NumberFormat();

/** Unknown cost is never shown as $0: the caller says "not available" instead. */
export function usd(value: string | number | null): string {
  return value === null ? "not available" : `$${String(value)}`;
}

export function UsagePage() {
  const { api } = useAuth();
  const [days, setDays] = useState(7);
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [budget, setBudget] = useState<Budget | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tokens, setTokens] = useState("");
  const [cost, setCost] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  // The form is seeded from the server once; later reloads (period change, retry) must not overwrite edits.
  const seeded = useRef(false);
  const edited = useRef(false);

  const load = useCallback(async () => {
    try {
      const [s, b] = await Promise.all([api.usageSummary(days), api.getBudget()]);
      setSummary(s);
      setBudget(b);
      if (!seeded.current && !edited.current) {
        seeded.current = true;
        setTokens(b.daily_token_limit === null ? "" : String(b.daily_token_limit));
        setCost(b.daily_cost_usd_limit ?? "");
      }
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [api, days]);

  useEffect(() => {
    void load();
  }, [load]);

  const persist = (t: number | null, c: number | null) => {
    setFormError(null);
    setSaved(false);
    setSaving(true);
    api
      .putBudget({ daily_token_limit: t, daily_cost_usd_limit: c })
      .then((b) => {
        // Show what the server confirmed, not what was typed.
        setBudget(b);
        setTokens(b.daily_token_limit === null ? "" : String(b.daily_token_limit));
        setCost(b.daily_cost_usd_limit ?? "");
        setSaved(true);
      })
      .catch((err: unknown) => {
        setError(err);
      })
      .finally(() => {
        setSaving(false);
      });
  };

  const save = (e: FormEvent) => {
    e.preventDefault();
    const t = parseLimit(tokens);
    const c = parseLimit(cost);
    if (t === "invalid" || c === "invalid" || (t !== null && !Number.isInteger(t))) {
      setFormError("Limits must be positive numbers (whole tokens), or empty for unlimited.");
      return;
    }
    const hasLimits = budget !== null && (budget.daily_token_limit !== null || budget.daily_cost_usd_limit !== null);
    if (t === null && c === null && hasLimits) {
      setConfirmClear(true);
      return;
    }
    persist(t, c);
  };

  const tokenPct =
    budget !== null && budget.daily_token_limit !== null ? Math.min(100, (budget.used_llm_tokens_today / budget.daily_token_limit) * 100) : null;

  return (
    <>
      <PageHeader
        title="Usage and budget"
        crumbs={[{ label: "Workspace", to: "/" }, { label: "Usage" }]}
        description="Language-model usage for your organization, and the daily limits that stop spending."
      />
      {error !== null && <ErrorNote error={error} onRetry={() => void load()} />}

      {budget === null && error === null && <Skeleton lines={3} label="Loading budget" />}
      {budget !== null && (
        <div className="stats">
          <div className="stat">
            <div className="stat-label">Tokens used today</div>
            <div className="stat-value">{nf.format(budget.used_llm_tokens_today)}</div>
            {tokenPct !== null && budget.daily_token_limit !== null ? (
              <>
                <progress max={100} value={tokenPct} aria-label="Share of the daily token limit used" />
                <p className="muted">of {nf.format(budget.daily_token_limit)} per day</p>
              </>
            ) : (
              <p className="muted">No daily token limit</p>
            )}
          </div>
          <div className="stat">
            <div className="stat-label">Known cost today</div>
            <div className="stat-value">{usd(budget.known_cost_usd_today)}</div>
            <p className="muted">
              {budget.daily_cost_usd_limit !== null ? `Limit ${usd(budget.daily_cost_usd_limit)} per day` : "No daily cost limit"}. Excludes usage the server
              could not price.
            </p>
          </div>
          <div className="stat">
            <div className="stat-label">Limits source</div>
            <div className="stat-value">{budget.source}</div>
            <p className="muted">Where the effective limits come from.</p>
          </div>
        </div>
      )}

      <Card title="Daily limits">
        <form onSubmit={save}>
          <div className="row-2">
            <div className="field">
              <label htmlFor="tokens">Tokens per day (empty = unlimited)</label>
              <input
                id="tokens"
                inputMode="numeric"
                value={tokens}
                onChange={(e) => {
                  edited.current = true;
                  setTokens(e.target.value);
                  setSaved(false);
                }}
              />
            </div>
            <div className="field">
              <label htmlFor="cost">Cost per day in USD (empty = unlimited)</label>
              <input
                id="cost"
                inputMode="decimal"
                value={cost}
                onChange={(e) => {
                  edited.current = true;
                  setCost(e.target.value);
                  setSaved(false);
                }}
              />
            </div>
          </div>
          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save limits"}
          </button>
        </form>
        {formError !== null && (
          <p role="alert" className="error">
            {formError}
          </p>
        )}
        {saved && (
          <p role="status" className="notice good">
            Limits saved.
          </p>
        )}
      </Card>

      <Card
        title="Summary"
        actions={
          <div>
            <label htmlFor="period" className="sr-only">
              Period
            </label>
            <select
              id="period"
              value={days}
              onChange={(e) => {
                setDays(Number(e.target.value));
              }}
            >
              {[1, 7, 30, 90].map((d) => (
                <option key={d} value={d}>
                  Last {d} day{d === 1 ? "" : "s"}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {summary === null && error === null && <Skeleton lines={4} label="Loading summary" />}
        {summary !== null && (
          <>
            <p className="muted">{summary.note}</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Day</th>
                    <th scope="col">Kind</th>
                    <th scope="col" className="num">
                      Events
                    </th>
                    <th scope="col" className="num">
                      Input tokens
                    </th>
                    <th scope="col" className="num">
                      Output tokens
                    </th>
                    <th scope="col" className="num">
                      Known cost (USD)
                    </th>
                    <th scope="col" className="num">
                      Unpriced events
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {summary.daily.length === 0 && (
                    <tr>
                      <td colSpan={7} className="muted">
                        No usage recorded in this period.
                      </td>
                    </tr>
                  )}
                  {summary.daily.map((d) => (
                    <tr key={`${d.day}-${d.kind}`}>
                      <td>{d.day}</td>
                      <td>{d.kind}</td>
                      <td className="num">{d.events}</td>
                      <td className="num">{d.input_tokens}</td>
                      <td className="num">{d.output_tokens}</td>
                      <td className="num">{d.known_cost_usd}</td>
                      <td className="num">{d.unpriced_events}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p>
              Average cost per priced query: {usd(summary.query_cost.average_cost_usd)} ({summary.query_cost.queries_counted} counted,{" "}
              {summary.query_cost.queries_excluded_unpriced} excluded as unpriced)
            </p>
          </>
        )}
      </Card>

      {confirmClear && (
        <ConfirmDialog
          title="Remove all spending limits?"
          confirmLabel="Remove limits"
          onCancel={() => {
            setConfirmClear(false);
          }}
          onConfirm={() => {
            setConfirmClear(false);
            persist(null, null);
          }}
        >
          <p>With both limits empty, nothing will stop your organization's language-model usage from growing. You can set limits again at any time.</p>
        </ConfirmDialog>
      )}
    </>
  );
}
