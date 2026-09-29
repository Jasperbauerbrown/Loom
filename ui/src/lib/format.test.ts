import { describe, expect, it } from "vitest";
import { compactNumber, compactionLabel, duration, normaliseNotePath, statusLabel, statusTitle } from "./format";

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

describe("compactNumber", () => {
  it("shows small counts exactly", () => {
    expect(compactNumber(0)).toBe("0");
    expect(compactNumber(410)).toBe("410");
    expect(compactNumber(999)).toBe("999");
  });
  it("abbreviates thousands and millions, dropping a whole .0", () => {
    expect(compactNumber(1000)).toBe("1k");
    expect(compactNumber(12345)).toBe("12.3k");
    expect(compactNumber(1234567)).toBe("1.2M");
  });
  it("is 0 for null/undefined/NaN", () => {
    expect(compactNumber(null)).toBe("0");
    expect(compactNumber(undefined)).toBe("0");
    expect(compactNumber(NaN)).toBe("0");
  });
});

describe("duration", () => {
  it("formats seconds, minutes, hours and days", () => {
    expect(duration("2026-09-22T10:00:00Z", "2026-09-22T10:00:12Z")).toBe("12s");
    expect(duration("2026-09-22T10:00:00Z", "2026-09-22T10:03:12Z")).toBe("3m 12s");
    expect(duration("2026-09-22T10:00:00Z", "2026-09-22T12:05:00Z")).toBe("2h 05m");
    expect(duration("2026-09-22T10:00:00Z", "2026-09-25T14:00:00Z")).toBe("3d 4h");
  });
  it("is empty without a start", () => {
    expect(duration(null)).toBe("");
  });
});
