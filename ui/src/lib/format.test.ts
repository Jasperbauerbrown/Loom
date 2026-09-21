import { describe, expect, it } from "vitest";
import { compactionLabel } from "./format";

describe("compactionLabel", () => {
  it("names each compaction kind", () => {
    expect(compactionLabel({ kind: "clear" }, "context cleared by user")).toBe("context cleared");
    expect(compactionLabel({ kind: "evict" }, "evicted 3 tool results")).toBe("old tool output stubbed");
  });

  it("counts folded events from meta or from the event line", () => {
    expect(compactionLabel({ kind: "fold", folded_count: 12 }, "")).toBe("context compacted (12 events summarised)");
    expect(compactionLabel({ kind: "fold" }, "folded 7 events into summary")).toBe(
      "context compacted (7 events summarised)",
    );
    expect(compactionLabel({ kind: "fold" }, "folded events into summary")).toBe("context compacted");
  });

  it("falls back for an unknown or missing kind", () => {
    expect(compactionLabel(undefined)).toBe("context compacted");
    expect(compactionLabel({ kind: "future" }, "")).toBe("context compacted");
  });
});
