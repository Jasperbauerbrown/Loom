import { describe, expect, it } from "vitest";
import { compactionLabel, normaliseNotePath, statusLabel, statusTitle } from "./format";

describe("statusLabel", () => {
  it("shows a thread that ended its turn without finishing as stopped, with a gloss", () => {
    expect(statusLabel("waiting_input")).toBe("stopped");
    expect(statusTitle("waiting_input")).toMatch(/without finishing/);
  });

  it("spaces every other status and glosses only the ones that need it", () => {
    expect(statusLabel("waiting_approval")).toBe("waiting approval");
    expect(statusLabel("running")).toBe("running");
    expect(statusLabel("done")).toBe("done");
    expect(statusTitle("done")).toBeUndefined();
  });
});

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

describe("normaliseNotePath", () => {
  it("adds .md, forward-slashes and strips a leading slash", () => {
    expect(normaliseNotePath("  projects\\music-bot ")).toBe("projects/music-bot.md");
    expect(normaliseNotePath("/devices/ender-3.md")).toBe("devices/ender-3.md");
  });
  it("is empty for empty input", () => {
    expect(normaliseNotePath("   ")).toBe("");
  });
});
