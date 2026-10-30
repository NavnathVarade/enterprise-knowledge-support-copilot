const IN_PROGRESS = new Set(["uploaded", "validating", "processing", "parsed", "chunked", "embedding", "indexing"]);

export function isInProgress(status: string): boolean {
  return IN_PROGRESS.has(status);
}

export function isFailure(status: string): boolean {
  return status.endsWith("_failed") || status === "quarantined";
}

export function statusLabel(status: string): string {
  if (status === "active") return "Ready";
  if (status === "quarantined") return "Blocked by safety checks";
  if (isFailure(status)) return "Failed";
  if (isInProgress(status)) return "Processing";
  return status;
}
