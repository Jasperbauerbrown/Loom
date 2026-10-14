import { describe, expect, it } from "vitest";
import { copyName, formatReasoning, parseReasoning } from "./presets";

describe("copyName", () => {
  it("appends -copy when that name is free", () => {
    expect(copyName("qwen-local", ["qwen-local", "other"])).toBe("qwen-local-copy");
  });

  it("numbers from 2 upward past every taken copy", () => {
    expect(copyName("qwen-local", ["qwen-local", "qwen-local-copy"])).toBe("qwen-local-copy-2");
    expect(copyName("qwen-local", ["qwen-local-copy", "qwen-local-copy-2", "qwen-local-copy-3"])).toBe("qwen-local-copy-4");
  });

  it("does not need the source itself in the taken list", () => {
    expect(copyName("fresh", [])).toBe("fresh-copy");
  });
});

describe("reasoning", () => {
  it("parses every value the model client understands", () => {
    expect(parseReasoning(undefined)).toEqual({ mode: "", budget: "" });
    expect(parseReasoning("")).toEqual({ mode: "", budget: "" });
    for (const v of ["off", "on", "low", "medium", "high"]) expect(parseReasoning(v)).toEqual({ mode: v, budget: "" });
    expect(parseReasoning("budget:16384")).toEqual({ mode: "budget", budget: "16384" });
  });

  it("reads an unknown string as medium effort, the way the client does", () => {
    expect(parseReasoning("extreme")).toEqual({ mode: "medium", budget: "" });
  });

  it("round-trips, and unset saves nothing so the provider default applies", () => {
    for (const v of ["off", "on", "low", "medium", "high", "budget:8192"]) {
      const { mode, budget } = parseReasoning(v);
      expect(formatReasoning(mode, budget)).toBe(v);
    }
    expect(formatReasoning("", "")).toBeUndefined();
    expect(formatReasoning("budget", "")).toBe("budget:0");
  });
});
