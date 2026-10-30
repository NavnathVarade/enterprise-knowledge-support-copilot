import { describe, expect, it } from "vitest";
import { passwordProblem, slugProblem, suggestSlug } from "./password";
import { safeRedirect } from "./redirect";

describe("password policy (mirrors the server)", () => {
  it.each([
    ["short1", "a@b.co", "at least 10"],
    ["onlyletterspassword", "a@b.co", "letter and one digit"],
    ["12345678901234", "a@b.co", "letter and one digit"],
    ["my-adalovelace-pass1", "adalovelace@x.co", "before the @"],
    ["é1".repeat(40), "a@b.co", "at most 72 bytes"],
  ])("rejects %s", (pw, email, fragment) => {
    expect(passwordProblem(pw, email)).toContain(fragment);
  });
  it("accepts a reasonable passphrase", () => {
    expect(passwordProblem("correct-horse-battery-9", "ada@example.com")).toBeNull();
  });
});

describe("organization identifier", () => {
  it("validates and suggests", () => {
    expect(slugProblem("acme")).toBeNull();
    expect(slugProblem("Acme")).not.toBeNull();
    expect(slugProblem("-acme")).not.toBeNull();
    expect(slugProblem("ab")).not.toBeNull();
    expect(suggestSlug("  Acme, Inc.  ")).toBe("acme-inc");
    expect(suggestSlug("Zürich Café")).toBe("zurich-cafe");
    expect(slugProblem(suggestSlug("Acme, Inc."))).toBeNull();
  });
});

describe("safeRedirect", () => {
  it.each([
    ["/documents", "/documents"],
    ["/audit?x=1", "/audit?x=1"],
    ["https://evil.example", "/"],
    ["//evil.example", "/"],
    ["/\\evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["/login", "/"],
    ["/signup", "/"],
    ["/a\nb", "/"],
    [undefined, "/"],
    [42, "/"],
  ])("%s -> %s", (input, out) => {
    expect(safeRedirect(input)).toBe(out);
  });
});
