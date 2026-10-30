import { afterEach, describe, expect, it, vi } from "vitest";
import { makeToken } from "../test/helpers";
import { clearSession, decodeClaims, isExpired, loadSession, saveSession } from "./session";

describe("decodeClaims", () => {
  it("reads roles and identity", () => {
    const c = decodeClaims(makeToken({ roles: ["admin", 5, "analyst"] }));
    expect(c).toMatchObject({ sub: "u1", email: "ada@example.com", tenantId: "t1", roles: ["admin", "analyst"] });
  });
  it("rejects malformed tokens", () => {
    for (const bad of ["", "a.b", "a.%%%.c", "a.bnVsbA.c", `a.${btoa("[]")}.c`, `a.${btoa('{"sub":1}')}.c`]) {
      expect(decodeClaims(bad)).toBeNull();
    }
  });
});

describe("session storage", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it("round-trips and expires", () => {
    const s = { token: makeToken(), expiresAt: Date.now() + 1000 };
    saveSession(s);
    expect(loadSession()).toEqual(s);
    expect(isExpired(s, s.expiresAt)).toBe(true);
    expect(loadSession(s.expiresAt + 1)).toBeNull();
    expect(sessionStorage.getItem("ekc.session")).toBeNull(); // expired entries are purged
  });
  it("drops tampered or garbage entries", () => {
    sessionStorage.setItem("ekc.session", "not json");
    expect(loadSession()).toBeNull();
    sessionStorage.setItem("ekc.session", JSON.stringify({ token: "x.y.z", expiresAt: Date.now() + 5000 }));
    expect(loadSession()).toBeNull();
  });
  it("survives blocked storage", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => { saveSession({ token: makeToken(), expiresAt: Date.now() + 1000 }); }).not.toThrow();
    expect(loadSession()).toBeNull();
    expect(() => { clearSession(); }).not.toThrow();
  });
});
