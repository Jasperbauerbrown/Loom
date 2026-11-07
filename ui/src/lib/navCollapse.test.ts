import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NARROW_PX, NAV_COLLAPSED_KEY, recallNavCollapsed, rememberNavCollapsed } from "./navCollapse";

beforeEach(() => {
  const mem = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => mem.get(k) ?? null,
    setItem: (k: string, v: string) => void mem.set(k, v),
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("navCollapse", () => {
  it("defaults by width when unset", () => {
    expect(recallNavCollapsed(NARROW_PX - 1)).toBe(true);
    expect(recallNavCollapsed(NARROW_PX)).toBe(false);
  });

  it("a remembered choice beats the width default", () => {
    rememberNavCollapsed(true);
    expect(recallNavCollapsed(1600)).toBe(true);
    rememberNavCollapsed(false);
    expect(recallNavCollapsed(320)).toBe(false);
  });

  it("survives blocked storage", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    });
    expect(() => rememberNavCollapsed(true)).not.toThrow();
    expect(recallNavCollapsed(320)).toBe(true);
  });

  it("ignores junk", () => {
    localStorage.setItem(NAV_COLLAPSED_KEY, "yes");
    expect(recallNavCollapsed(1600)).toBe(false);
  });
});
