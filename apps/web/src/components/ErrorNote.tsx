import { ApiError } from "../api/client";

export function describeError(err: unknown): { message: string; requestId: string | null; transient: boolean } {
  if (err instanceof ApiError) {
    const wait = err.retryAfterSeconds !== null && err.retryAfterSeconds > 0 ? ` Try again in ${String(Math.ceil(err.retryAfterSeconds))}s.` : "";
    return { message: `${err.message}${wait}`, requestId: err.requestId, transient: err.transient };
  }
  return { message: "Something went wrong. Try again.", requestId: null, transient: false };
}

export function ErrorNote({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  const d = describeError(error);
  return (
    <div role="alert" className="error">
      {title !== undefined && (
        <p>
          <strong>{title}</strong>
        </p>
      )}
      <p>{d.message}</p>
      {d.requestId !== null && <p className="muted">Reference: {d.requestId}</p>}
      {d.transient && onRetry !== undefined && (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}
