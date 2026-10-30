import { describe, expect, it } from "vitest";
import { isFailure, isInProgress, statusLabel } from "./documents";
import { formatCell } from "./format";
import { MAX_UPLOAD_BYTES, validateFile } from "./upload";

describe("validateFile", () => {
  it("accepts supported types case-insensitively", () => {
    expect(validateFile({ name: "Report.PDF", size: 10 })).toBeNull();
    expect(validateFile({ name: "notes.md", size: 10 })).toBeNull();
  });
  it("rejects unsupported, extensionless, double-extension executables, empty and oversized files", () => {
    expect(validateFile({ name: "run.exe", size: 10 })).toMatch(/Unsupported/);
    expect(validateFile({ name: "README", size: 10 })).toMatch(/Unsupported/);
    expect(validateFile({ name: "doc.pdf.exe", size: 10 })).toMatch(/Unsupported/);
    expect(validateFile({ name: "a.txt", size: 0 })).toMatch(/empty/);
    expect(validateFile({ name: "a.txt", size: MAX_UPLOAD_BYTES + 1 })).toMatch(/larger/);
    expect(validateFile({ name: "a.txt", size: MAX_UPLOAD_BYTES })).toBeNull();
  });
});

describe("document status", () => {
  it("classifies every lifecycle state", () => {
    for (const s of ["uploaded", "validating", "processing", "parsed", "chunked", "embedding", "indexing"]) {
      expect(isInProgress(s)).toBe(true);
    }
    for (const s of ["processing_failed", "embedding_failed", "indexing_failed", "quarantined"]) {
      expect(isFailure(s)).toBe(true);
      expect(isInProgress(s)).toBe(false);
    }
    expect([statusLabel("active"), statusLabel("archived")]).toEqual(["Ready", "archived"]);
  });
});

describe("formatCell", () => {
  it("renders values as text, never markup", () => {
    expect(formatCell(null)).toBe("");
    expect(formatCell({ a: 1 })).toBe('{"a":1}');
    expect(formatCell("<img src=x onerror=alert(1)>")).toBe("<img src=x onerror=alert(1)>");
  });
});
