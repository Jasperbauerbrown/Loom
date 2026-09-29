import { describe, expect, it } from "vitest";
import { copyName } from "./presets";

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
