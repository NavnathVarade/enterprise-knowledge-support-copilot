import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import type { DocumentSummary } from "../api/types";
import { ErrorNote } from "../components/ErrorNote";
import { Icon } from "../components/Icon";
import { Badge, Card, EmptyState, PageHeader, Skeleton } from "../components/ui";
import type { Tone } from "../components/ui";
import { useAuth } from "../auth/AuthContext";
import { isFailure, isInProgress, statusLabel } from "../lib/documents";
import { ACCEPT_ATTRIBUTE, ALLOWED_TYPES, MAX_UPLOAD_BYTES, validateFile } from "../lib/upload";

const POLL_MS = 5000;

function tone(status: string): Tone {
  if (status === "active") return "good";
  if (isFailure(status)) return "bad";
  if (isInProgress(status)) return "info";
  return "neutral";
}

export function DocumentsPage({ pollMs = POLL_MS }: { pollMs?: number }) {
  const { api } = useAuth();
  const [docs, setDocs] = useState<DocumentSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadedName, setUploadedName] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setDocs(await api.listDocuments());
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while something is still being processed; the timer is cleared on change/unmount.
  const pending = docs?.some((d) => isInProgress(d.status)) ?? false;
  useEffect(() => {
    if (!pending) return;
    const t = setTimeout(() => {
      void refresh();
    }, pollMs);
    return () => {
      clearTimeout(t);
    };
  }, [pending, docs, refresh, pollMs]);

  const upload = (file: File) => {
    const problem = validateFile(file);
    if (problem !== null) {
      setUploadError(problem);
      setUploadedName(null);
      return;
    }
    setUploadError(null);
    setUploadedName(null);
    setUploading(true);
    api
      .uploadDocument(file)
      .then(() => {
        setUploadedName(file.name);
        return refresh();
      })
      .catch((err: unknown) => {
        setUploadError(err instanceof Error ? err.message : "Upload failed.");
      })
      .finally(() => {
        setUploading(false);
        if (input.current) input.current.value = "";
      });
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file === undefined) return;
    const invalid = validateFile(file) !== null;
    upload(file);
    if (invalid) e.target.value = "";
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setOver(false);
    const file = e.dataTransfer.files[0];
    if (file !== undefined && !uploading) upload(file);
  };

  const counts = { ready: 0, processing: 0, failed: 0 };
  for (const d of docs ?? []) {
    if (d.status === "active") counts.ready += 1;
    else if (isFailure(d.status)) counts.failed += 1;
    else if (isInProgress(d.status)) counts.processing += 1;
  }

  return (
    <>
      <PageHeader
        title="Documents"
        crumbs={[{ label: "Workspace", to: "/" }, { label: "Documents" }]}
        description="Upload files to make them searchable. Each one is checked, split and indexed before it can be cited."
        actions={
          <button type="button" className="secondary" onClick={() => void refresh()}>
            <Icon name="refresh" size={16} />
            Refresh
          </button>
        }
      />
      <div
        className={`dropzone${over ? " over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => {
          setOver(false);
        }}
        onDrop={onDrop}
      >
        <Icon name="upload" size={28} />
        <label className="button">
          {uploading ? "Uploading…" : "Upload a document"}
          <input ref={input} type="file" accept={ACCEPT_ATTRIBUTE} onChange={onPick} disabled={uploading} hidden />
        </label>
        <p className="hint">
          or drop a file here. {Object.keys(ALLOWED_TYPES).join(", ")} up to {String(MAX_UPLOAD_BYTES / (1024 * 1024))} MB.
        </p>
        {uploading && <progress aria-label="Uploading" />}
      </div>
      {uploadedName !== null && (
        <p role="status" className="notice good">
          {uploadedName} was accepted and is being processed.
        </p>
      )}
      {uploadError !== null && (
        <p role="alert" className="error">
          {uploadError}
        </p>
      )}
      {error !== null && <ErrorNote error={error} onRetry={() => void refresh()} />}
      {docs === null && error === null && <Skeleton lines={4} label="Loading documents" />}
      {docs !== null && docs.length === 0 && (
        <EmptyState title="No documents yet." icon="documents">
          Upload your first document to start asking questions about it.
        </EmptyState>
      )}
      {docs !== null && docs.length > 0 && (
        <Card title="Your documents">
          <div className="status-row">
            <span className="chip">{counts.ready} ready</span>
            <span className="chip">{counts.processing} in progress</span>
            <span className="chip">{counts.failed} failed or blocked</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Title</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td>{d.title}</td>
                    <td>
                      <Badge tone={tone(d.status)}>{statusLabel(d.status)}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pending && <p className="muted">Checking again every {String(pollMs / 1000)} seconds while documents are processing.</p>}
        </Card>
      )}
    </>
  );
}
