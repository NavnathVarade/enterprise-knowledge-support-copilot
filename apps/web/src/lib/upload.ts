// Client-side pre-checks so people get instant feedback. The server re-validates everything
// (extension, declared type, size, content sniffing, parser limits): never rely on this.

export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export const ALLOWED_TYPES: Readonly<Record<string, string>> = {
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".csv": "text/csv",
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".json": "application/json",
};

export const ACCEPT_ATTRIBUTE = Object.keys(ALLOWED_TYPES).join(",");

export function validateFile(file: { name: string; size: number }): string | null {
  const dot = file.name.lastIndexOf(".");
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!(ext in ALLOWED_TYPES)) {
    return `Unsupported file type. Allowed: ${Object.keys(ALLOWED_TYPES).join(", ")}.`;
  }
  if (file.size === 0) return "That file is empty.";
  if (file.size > MAX_UPLOAD_BYTES) {
    return `That file is larger than ${String(MAX_UPLOAD_BYTES / 1024 / 1024)} MB.`;
  }
  return null;
}
